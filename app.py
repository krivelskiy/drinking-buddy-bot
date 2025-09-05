import os
import json
import asyncio
import logging
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====== ЛОГИ ======
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("app")

# ====== ENV ======
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

# где взять внешний URL (Render сам пробрасывает переменную)
WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or os.getenv("PRIMARY_HOSTNAME")
    or ""
).strip()

# ====== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ======
app = FastAPI()
_engine: Optional[Engine] = None
_tapp: Optional[Application] = None

# ----- OpenAI (синхронный клиент, дергаем из отдельного потока) -----
_openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI

        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
    except Exception as e:
        logger.exception("OpenAI init failed: %s", e)
else:
    logger.warning("⚠️ OPENAI_API_KEY is empty (диалоги будут отвечать заглушкой)")

# ====== БАЗА ДАННЫХ ======
def db() -> Optional[Engine]:
    """Ленивая инициализация Engine. Схему НЕ создаём, ничего не мигрируем."""
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            logger.warning("⚠️ DATABASE_URL is empty (память/профили пользователей недоступны)")
            return None
        _engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_recycle=300,
            future=True,
        )
        logger.info("✅ Database initialized")
    return _engine


async def fetch_user_profile(chat_id: int) -> Dict[str, Any]:
    """
    Аккуратно читаем профиль из существующей таблицы users.
    Никаких предположений о точных названиях колонок — определяем по факту.
    Ничего не создаём и не модифицируем.
    """
    eng = db()
    if not eng:
        return {}

    # Вытащим список колонок
    with eng.connect() as conn:
        cols = set(
            r[0]
            for r in conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'users'
                    """
                )
            ).all()
        )

        # Определяем поле для chat_id
        chat_id_col_candidates = ["chat_id", "telegram_id", "tg_chat_id"]
        chat_id_col = next((c for c in chat_id_col_candidates if c in cols), None)
        if not chat_id_col:
            logger.warning("users table has no chat_id-like column; columns=%s", cols)
            return {}

        row = conn.execute(
            text(f"SELECT * FROM public.users WHERE {chat_id_col} = :cid LIMIT 1"),
            {"cid": chat_id},
        ).mappings().first()

        if not row:
            return {}

        # Сопоставляем возможные поля
        name_cols = ["display_name", "name", "full_name", "first_name", "username"]
        age_cols = ["age", "years"]
        gender_cols = ["gender", "sex"]
        stars_cols = ["stars", "gifts", "gift_stars", "balance", "coins"]

        def pick(keys: List[str]) -> Optional[Any]:
            for k in keys:
                if k in row and row[k] is not None:
                    return row[k]
            return None

        profile = {
            "name": pick(name_cols),
            "age": pick(age_cols),
            "gender": pick(gender_cols),
            "stars": pick(stars_cols),
            "raw": dict(row),
        }
        return profile


# ====== OPENAI ДИАЛОГ ======
AI_STUB = "🤖 Сейчас я не могу поговорить: нет связи с мозгом. Попробуй позже."

async def ask_openai(user_text: str, profile: Dict[str, Any]) -> Optional[str]:
    """
    Все диалоги ТОЛЬКО через OpenAI.
    Если клиента нет или ошибка — возвращаем None, а сверху отправим заглушку.
    Никаких зеркалок/эхо.
    """
    if not _openai_client:
        return None

    # Сбор persona + память
    memory_bits: List[str] = []
    if profile:
        if profile.get("name"):
            memory_bits.append(f"Имя пользователя: {profile['name']}")
        if profile.get("age"):
            memory_bits.append(f"Возраст пользователя: {profile['age']}")
        if profile.get("gender"):
            memory_bits.append(f"Пол пользователя: {profile['gender']}")

    memory_block = "\n".join(memory_bits) if memory_bits else "Информация о пользователе отсутствует."

    system_prompt = (
        "Ты «Катя Собутыльница» — тёплая, дружелюбная русскоязычная собеседница, коротко и по делу, без занудства. "
        "Отвечай эмпатично, но не многословно (1–3 предложения). Избегай повторов вопросов пользователя."
        "\n\n"
        f"{memory_block}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    try:
        # openai client v1 — синхронный вызов, поэтому уводим в отдельный поток
        def _call():
            resp = _openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.7,
                max_tokens=300,
            )
            return resp.choices[0].message.content.strip()

        return await asyncio.to_thread(_call)
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return None


# ====== TELEGRAM HANDLERS ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я Катя Собутыльница 🍷\n"
        "Пиши, поболтаем. Если вдруг я пропаду — значит у меня нет связи с мозгом (OpenAI), тогда я честно скажу об этом.",
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_text = (update.message.text or "").strip()

    # Загружаем профиль из БД (НЕ меняем схему)
    profile = await asyncio.to_thread(fetch_user_profile, chat_id)

    # Спрашиваем OpenAI; если не получилось — заглушка
    reply = await ask_openai(user_text, profile)
    if not reply:
        await update.message.reply_text(AI_STUB)
        return

    await update.message.reply_text(f"🤖 {reply}")


def build_telegram_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))

    # Любой текст → в OpenAI (или заглушка)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return application


# ====== FASTAPI LIFECYCLE ======
@app.on_event("startup")
async def on_startup():
    global _tapp
    # Инициализируем БД (лениво), чтобы в логах видеть статус
    _ = db()

    # Telegram
    try:
        _tapp = build_telegram_app()
        await _tapp.initialize()
        await _tapp.start()

        # Настраиваем webhook, если есть внешний URL
        if WEBHOOK_BASE_URL:
            url = WEBHOOK_BASE_URL.rstrip("/") + f"/webhook/{TELEGRAM_BOT_TOKEN}"
            await _tapp.bot.set_webhook(url=url)
            logger.info("✅ Webhook set to %s", url)
        else:
            logger.warning("⚠️ WEBHOOK_BASE_URL is empty — webhook не будет настроен")

        logger.info("✅ Telegram application started")
    except Exception as e:
        logger.exception("Startup failed: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    global _tapp
    if _tapp:
        try:
            await _tapp.stop()
            await _tapp.shutdown()
        except Exception:
            pass


# ====== HTTP ENDPOINTS ======
@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "OK"

@app.head("/", response_class=PlainTextResponse)
async def root_head() -> str:
    return "OK"

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request) -> Response:
    if token != TELEGRAM_BOT_TOKEN:
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    if not _tapp:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    data = await request.json()
    try:
        # просто логируем update_id для наглядности
        upd_id = data.get("update_id")
        if upd_id is not None:
            logger.info("Incoming update_id=%s", upd_id)

        update = Update.de_json(data=data, bot=_tapp.bot)
        await _tapp.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.exception("Webhook processing error: %s", e)
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

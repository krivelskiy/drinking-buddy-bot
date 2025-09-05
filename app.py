import os
import logging
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from sqlalchemy import create_engine, text, inspect, MetaData, Table
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------------------
# ЛОГИРОВАНИЕ
# ---------------------------
logger = logging.getLogger("app")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# ---------------------------
# КОНФИГ
# ---------------------------
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "true").lower() in ("1", "true", "yes", "y")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()  # <-- ТОЛЬКО ЭТОТ КЛЮЧ
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

OPENAI_FALLBACK = "Извини, у меня временные неполадки с мозгами 🤖. Попробуй позже."

# ---------------------------
# БАЗА ДАННЫХ
# ---------------------------
def build_engine() -> Engine:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    eng = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        future=True,
    )
    return eng

engine: Engine = build_engine()

# ленивое отражение таблицы users (чтобы не рисковать несоответствием схемы)
_metadata = MetaData()
_users_table: Optional[Table] = None


def get_users_table() -> Table:
    global _users_table
    if _users_table is not None:
        return _users_table

    # Пытаемся сначала со схемой public, потом без схемы (на всякий)
    try:
        _metadata.clear()
        _users_table = Table("users", _metadata, autoload_with=engine, schema="public")
        return _users_table
    except Exception:
        _metadata.clear()
        _users_table = Table("users", _metadata, autoload_with=engine)
        return _users_table


def ensure_user(chat_id: int) -> Dict[str, Any]:
    """
    Находит пользователя по chat_id. Если нет — создаёт минимальную запись.
    Ничего в схеме не предполагаем жёстко: используем только существующие поля.
    """
    users = get_users_table()
    cols = {c.name for c in users.columns}

    with engine.begin() as conn:
        # ищем по chat_id
        if "chat_id" in cols:
            row = conn.execute(text("SELECT * FROM users WHERE chat_id = :cid LIMIT 1"), {"cid": chat_id}).mappings().first()
        else:
            row = None

        if row:
            return dict(row)

        # если нет записи, пробуем вставить, только если есть поле chat_id
        if "chat_id" in cols:
            insert_sql = "INSERT INTO users (chat_id) VALUES (:cid) RETURNING *"
            row = conn.execute(text(insert_sql), {"cid": chat_id}).mappings().first()
            return dict(row) if row else {}
        # если таблица без chat_id — ничего не делаем
        return {}


# ---------------------------
# OpenAI
# ---------------------------
openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI

        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
    except Exception as e:
        logger.warning("OpenAI init failed: %s", e)


async def ask_openai(text_in: str, user_row: Dict[str, Any]) -> str:
    """
    Все диалоги только через OpenAI. Если не доступен — фиксированная заглушка.
    Никаких эхо.
    """
    if not openai_client:
        return OPENAI_FALLBACK

    # Подготавливаем простейший профиль из БД (если есть нужные поля)
    name = user_row.get("name") or user_row.get("first_name") or ""
    age = user_row.get("age")
    persona = "Ты дружелюбная собутыльница Катя. Отвечай кратко и по-доброму, на русском."

    if name:
        persona += f" Собеседника зовут {name}."
    if age:
        persona += f" Ему {age} лет."

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": persona},
                {"role": "user", "content": text_in},
            ],
            temperature=0.7,
        )
        return (resp.choices[0].message.content or "").strip() or OPENAI_FALLBACK
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        return OPENAI_FALLBACK


# ---------------------------
# Telegram (python-telegram-bot 20.x)
# ---------------------------
telegram_app: Optional[Application] = None


def mask_token(tok: str) -> str:
    if not tok:
        return "<empty>"
    if len(tok) <= 10:
        return "***" + tok[-4:]
    return tok[:6] + "..." + tok[-6:]


def build_telegram_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    # никаких эхо/тестовых хендлеров
    return app


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        ensure_user(chat_id)
        await update.message.reply_text("Привет! Я Катя 🍸 Готова поболтать.")
    except Exception as e:
        logger.error("start_handler error: %s", e)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return
        chat_id = update.effective_chat.id
        user_row = ensure_user(chat_id)
        user_text = update.message.text.strip()
        answer = await ask_openai(user_text, user_row)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error("text_handler error: %s", e)
        await update.message.reply_text(OPENAI_FALLBACK)


# ---------------------------
# FastAPI app
# ---------------------------
app = FastAPI(title="Drinking Buddy Bot", version="1.0.0")


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "ok": True,
        "webhook_expected": bool(BOT_TOKEN and APP_BASE_URL),
        "auto_set_webhook": AUTO_SET_WEBHOOK,
        "bot_token_masked": mask_token(BOT_TOKEN),
    }


@app.on_event("startup")
async def on_startup():
    # проверим базу
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ Database initialized")
    except SQLAlchemyError as e:
        logger.error("Database init failed: %s", e)
        raise

    # Telegram
    global telegram_app
    if not BOT_TOKEN:
        logger.error("Startup failed: BOT_TOKEN is not set")
        return

    telegram_app = build_telegram_app()
    # для работы process_update требуется initialize()
    await telegram_app.initialize()

    # Вебхук выставляем только если явно разрешено и известен base URL
    if AUTO_SET_WEBHOOK and APP_BASE_URL:
        url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
        try:
            await telegram_app.bot.set_webhook(url=url)
            logger.info("✅ Webhook set to %s", url)
        except Exception as e:
            logger.error("Set webhook failed: %s", e)
    else:
        logger.warning("Webhook NOT set (AUTO_SET_WEBHOOK=%s, APP_BASE_URL=%s)", AUTO_SET_WEBHOOK, APP_BASE_URL)


@app.on_event("shutdown")
async def on_shutdown():
    global telegram_app
    if telegram_app:
        try:
            await telegram_app.shutdown()
        except Exception:
            pass


# ---------------------------
# ВЕБХУК
# ---------------------------
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if not BOT_TOKEN:
        return Response(status_code=403, content="BOT_TOKEN not set")
    if token != BOT_TOKEN:
        # защищаемся от чужих/старых токенов
        return Response(status_code=403, content="wrong token")

    if not telegram_app:
        return Response(status_code=503, content="telegram app not ready")

    data = await request.json()
    try:
        update = Update.de_json(data, bot=telegram_app.bot)
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("Webhook process error: %s", e)
        return Response(status_code=500, content="update processing failed")


# ---------------------------
# DEBUG: СХЕМА БАЗЫ
# ---------------------------
@app.get("/debug/users-schema")
def debug_users_schema():
    """Возвращает структуру таблицы users (колонки, типы, nullable, default)."""
    try:
        insp = inspect(engine)
        # пробуем со схемой public, если нет — без схемы
        try:
            cols = insp.get_columns("users", schema="public")
        except Exception:
            cols = insp.get_columns("users")

        out = []
        for c in cols:
            out.append(
                {
                    "name": c.get("name"),
                    "type": str(c.get("type")),
                    "nullable": bool(c.get("nullable")),
                    "default": str(c.get("default")),
                }
            )
        return {"users": out}
    except Exception as e:
        logger.error("/debug/users-schema error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

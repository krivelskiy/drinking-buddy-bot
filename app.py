import os
import logging
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import JSONResponse

from sqlalchemy import create_engine, text, inspect, MetaData, Table
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------------------
# ЛОГИРОВАНИЕ
# ---------------------------
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# ---------------------------
# КОНФИГ (ТОЛЬКО ТАКИЕ КЛЮЧИ!)
# ---------------------------
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "true").lower() in ("1", "true", "yes", "y")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

OPENAI_FALLBACK = "Извини, у меня временные неполадки с мозгами 🤖. Попробуй позже."

# ---------------------------
# БАЗА ДАННЫХ
# ---------------------------
def build_engine() -> Engine:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

engine: Engine = build_engine()

_metadata = MetaData()
_users_table: Optional[Table] = None


def get_users_table() -> Table:
    """Ленивое отражение таблицы users (не шьём схему в код)."""
    global _users_table
    if _users_table is not None:
        return _users_table
    try:
        _metadata.clear()
        _users_table = Table("users", _metadata, autoload_with=engine, schema="public")
    except Exception:
        _metadata.clear()
        _users_table = Table("users", _metadata, autoload_with=engine)
    return _users_table


def upsert_user_from_tg(update: Update) -> Dict[str, Any]:
    """
    Приводим БД в актуальное состояние под схему:
    chat_id BIGINT not null, tg_id BIGINT not null, username, first_name, last_name,
    free_drinks INT default 0, favorite_drinks JSONB default [].
    """
    chat = update.effective_chat
    if not chat:
        return {}

    chat_id = int(chat.id)
    tg_id = int(getattr(update.effective_user, "id", chat_id) or chat_id)
    username = getattr(update.effective_user, "username", None)
    first_name = getattr(update.effective_user, "first_name", None)
    last_name = getattr(update.effective_user, "last_name", None)

    users = get_users_table()
    with engine.begin() as conn:
        # ищем по chat_id (основной ключ), если вдруг нет — по tg_id
        row = conn.execute(
            text("SELECT * FROM users WHERE chat_id = :cid LIMIT 1"),
            {"cid": chat_id},
        ).mappings().first()
        if not row:
            row = conn.execute(
                text("SELECT * FROM users WHERE tg_id = :tid LIMIT 1"),
                {"tid": tg_id},
            ).mappings().first()

        if row:
            # обновляем tg-поля и updated_at
            conn.execute(
                text(
                    """
                    UPDATE users
                       SET tg_id      = :tg_id,
                           username   = :username,
                           first_name = :first_name,
                           last_name  = :last_name,
                           updated_at = now()
                     WHERE chat_id    = :chat_id
                    """
                ),
                {
                    "tg_id": tg_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "chat_id": row["chat_id"],  # уже существующий chat_id
                },
            )
            # перечитываем
            row = conn.execute(
                text("SELECT * FROM users WHERE chat_id = :cid LIMIT 1"),
                {"cid": row["chat_id"]},
            ).mappings().first()
            return dict(row)

        # не нашли — создаём корректную запись
        row = conn.execute(
            text(
                """
                INSERT INTO users (chat_id, tg_id, username, first_name, last_name)
                VALUES (:chat_id, :tg_id, :username, :first_name, :last_name)
                RETURNING *
                """
            ),
            {
                "chat_id": chat_id,
                "tg_id": tg_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
            },
        ).mappings().first()
        return dict(row) if row else {}


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
    if not openai_client:
        return OPENAI_FALLBACK

    # Собираем «память» из БД
    name = user_row.get("name") or user_row.get("first_name") or ""
    summary = (user_row.get("summary") or "").strip()
    favs = user_row.get("favorite_drinks")
    try:
        favs_str = ""
        if isinstance(favs, list) and favs:
            favs_str = " Любимые напитки: " + ", ".join(map(str, favs)) + "."
    except Exception:
        favs_str = ""

    persona = "Ты дружелюбная собутыльница Катя. Отвечай кратко и по-доброму, на русском."
    if name:
        persona += f" Собеседника зовут {name}."
    if summary:
        persona += f" Краткая инфа о нём: {summary}."
    if favs_str:
        persona += favs_str

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
    return tok[:6] + "..." + tok[-6:] if len(tok) > 12 else "***" + tok[-4:]


def build_telegram_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        row = upsert_user_from_tg(update)
        name = row.get("name") or row.get("first_name") or ""
        hi = f"Привет, {name}! " if name else "Привет! "
        await update.message.reply_text(hi + "Я Катя 🍸 Готова поболтать.")
    except Exception as e:
        logger.error("start_handler error: %s", e)
        await update.message.reply_text(OPENAI_FALLBACK)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return
        row = upsert_user_from_tg(update)
        user_text = update.message.text.strip()
        answer = await ask_openai(user_text, row)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error("text_handler error: %s", e)
        await update.message.reply_text(OPENAI_FALLBACK)


# ---------------------------
# FastAPI app
# ---------------------------
app = FastAPI(title="Drinking Buddy Bot", version="1.1.0")


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
    # DB ping
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
    await telegram_app.initialize()

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
# DEBUG ЭНДПОИНТЫ
# ---------------------------
@app.get("/debug/users-schema")
def debug_users_schema():
    try:
        insp = inspect(engine)
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


@app.get("/debug/user")
def debug_user(chat_id: int = Query(..., description="Telegram chat_id")):
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM users WHERE chat_id = :cid LIMIT 1"),
                {"cid": chat_id},
            ).mappings().first()
            if not row:
                row = conn.execute(
                    text("SELECT * FROM users WHERE tg_id = :cid LIMIT 1"),
                    {"cid": chat_id},
                ).mappings().first()
        return {"user": dict(row) if row else None}
    except Exception as e:
        logger.error("/debug/user error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

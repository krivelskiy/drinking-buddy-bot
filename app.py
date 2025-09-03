import os
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from openai import OpenAI

from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import sessionmaker, declarative_base

# ---------------------------
# ЛОГИРОВАНИЕ
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("app")

# ---------------------------
# ОКРУЖЕНИЕ
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")  # напр. https://drinking-buddy-bot.onrender.com
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./memory.db")

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN is missing")
if not OPENAI_API_KEY:
    logger.error("❌ OPENAI_API_KEY is missing")
if not APP_BASE_URL:
    logger.warning("⚠️ APP_BASE_URL is missing (авто-установка вебхука будет пропущена)")

# ---------------------------
# OpenAI
# ---------------------------
client: Optional[OpenAI] = None
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("✅ OpenAI client initialized")
except Exception as e:
    logger.exception("❌ OpenAI init failed: %s", e)

# ---------------------------
# БАЗА (SQLAlchemy)
# ---------------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class UserMemory(Base):
    __tablename__ = "user_memory"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, unique=True)
    name = Column(String(100))
    favorite_drink = Column(String(100))
    history = Column(Text)


try:
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Database initialized")
except Exception as e:
    logger.exception("❌ Database init failed: %s", e)

# ---------------------------
# Telegram bot (PTB v20)
# ---------------------------
tapp = Application.builder().token(BOT_TOKEN).build()

# Стикеры
STICKERS = {
    "водка": "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "виски": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "вино":  "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "пиво":  "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
    "грусть":  "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "веселье": "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
}

# ---------------------------
# Хелперы памяти
# ---------------------------
def _save_history(session, user_id: int, user_name: str, user_text: str, bot_text: str | None = None):
    mem = session.query(UserMemory).filter_by(user_id=user_id).first()
    if not mem:
        mem = UserMemory(user_id=user_id, name=user_name, favorite_drink="", history="")
        session.add(mem)
        session.flush()
    if user_text:
        mem.history = (mem.history or "") + f"\nUser: {user_text}"
    if bot_text:
        mem.history = (mem.history or "") + f"\nBot: {bot_text}"
    session.commit()
    return mem


def _maybe_extract_favorite_drink(mem: UserMemory, text: str) -> None:
    low = text.lower()
    if "любим" in low:
        for drink in ("пиво", "вино", "водка", "виски"):
            if drink in low:
                mem.favorite_drink = drink


# ---------------------------
# Хендлеры
# ---------------------------
async def start(update: Update, context):
    try:
        text = "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
        logger.info("Sent /start greet to chat %s", update.effective_chat.id)
    except Exception:
        logger.exception("Failed to handle /start")


async def handle_message(update: Update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    user_name = user.first_name
    user_text = update.message.text or ""

    # Стикеры по ключевым словам
    for key, sid in STICKERS.items():
        if key in user_text.lower():
            try:
                await context.bot.send_sticker(chat_id=chat_id, sticker=sid)
                logger.info("Sticker sent (%s) to chat %s", key, chat_id)
            except Exception:
                logger.exception("Failed sending sticker %s", key)

    # Память
    session = SessionLocal()
    try:
        mem = _save_history(session, user_id, user_name, user_text)
        _maybe_extract_favorite_drink(mem, user_text)
        session.commit()

        # Ответ от модели (или fallback)
        try:
            if client is None:
                raise RuntimeError("OpenAI client not initialized")

            short_history = (mem.history or "").splitlines()[-20:]
            system_prompt = (
                "Ты — Катя, собутыльница. Женский тон, лёгкий флирт (уместно), юмор, "
                "дружелюбие; даёшь мягкие психологические советы и поддерживаешь диалог. "
                "Помни известные факты о собеседнике (имя, любимый напиток), "
                "но не повторяй одно и то же в каждом сообщении."
            )
            messages = [{"role": "system", "content": system_prompt}]
            for line in short_history:
                role = "user" if line.startswith("User:") else "assistant"
                messages.append({"role": role, "content": line.split(": ", 1)[1] if ": " in line else line})

            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
            )
            response_text = (completion.choices[0].message.content or "").strip()
        except Exception as e:
            logger.exception("OpenAI error")
            response_text = "Эх, давай просто выпьем за всё хорошее! 🥃"

        _save_history(session, user_id, user_name, "", response_text)
    finally:
        session.close()

    try:
        await context.bot.send_message(chat_id=chat_id, text=response_text)
    except Exception:
        logger.exception("Failed to send message to chat %s", chat_id)


# Регистрация хендлеров
tapp.add_handler(CommandHandler("start", start))
tapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ---------------------------
# FastAPI
# ---------------------------
app = FastAPI()

@app.on_event("startup")
async def _startup():
    """
    ВАЖНО: для ручной обработки вебхуков через FastAPI нужно явно инициализировать PTB Application.
    """
    try:
        await tapp.initialize()
        logger.info("✅ PTB Application initialized")
    except Exception:
        logger.exception("❌ PTB Application initialize failed")

    # Ставим вебхук, если есть базовый URL
    if APP_BASE_URL:
        try:
            wh_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
            await tapp.bot.set_webhook(url=wh_url, allowed_updates=["message"])
            logger.info("✅ Webhook set to %s", wh_url)
        except Exception:
            logger.exception("❌ set_webhook failed")


@app.on_event("shutdown")
async def _shutdown():
    try:
        await tapp.shutdown()
        logger.info("✅ PTB Application shutdown")
    except Exception:
        logger.exception("❌ PTB Application shutdown failed")


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        logger.warning("Webhook hit with wrong token")
        return JSONResponse(status_code=403, content={"ok": False, "error": "Forbidden"})

    try:
        data = await request.json()
        update = Update.de_json(data, tapp.bot)  # PTB v20: обязателен bot вторым аргументом
        logger.info("Incoming update_id=%s", getattr(update, "update_id", "n/a"))
        try:
            await tapp.process_update(update)  # требует .initialize() (см. startup)
        except Exception as e:
            logger.exception("process_update failed")
            # Не шлём ответ в чат из вебхука (чтобы не зациклить/заспамить), просто 500 → Telegram перешлёт повтор
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        return JSONResponse(content={"ok": True})
    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/")
async def health():
    return {"status": "ok"}

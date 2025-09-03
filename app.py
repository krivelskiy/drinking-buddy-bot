import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from openai import OpenAI
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# -------------------------------------------------
# Логирование
# -------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------------------------
# Настройки
# -------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

if not all([BOT_TOKEN, OPENAI_API_KEY, APP_BASE_URL, DATABASE_URL]):
    logger.error("❌ Переменные окружения не заданы полностью.")
    raise RuntimeError("Missing environment variables")

# -------------------------------------------------
# OpenAI клиент
# -------------------------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("✅ OpenAI client initialized")

# -------------------------------------------------
# SQLAlchemy
# -------------------------------------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class UserMemory(Base):
    __tablename__ = "user_memory"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True)
    name = Column(String, nullable=True)
    drinks = Column(String, nullable=True)
    history = Column(Text, default="")  # вся история общения


Base.metadata.create_all(bind=engine)
logger.info("✅ Database initialized")

# -------------------------------------------------
# Telegram bot
# -------------------------------------------------
telegram_app = Application.builder().token(BOT_TOKEN).build()


async def start(update: Update, context):
    user_id = update.effective_user.id
    session = SessionLocal()
    user = session.query(UserMemory).filter_by(user_id=user_id).first()

    if not user:
        user = UserMemory(user_id=user_id, history="")
        session.add(user)
        session.commit()

    await update.message.reply_text("Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?")
    session.close()


async def toast(update: Update, context):
    await update.message.reply_text("За здоровье и весёлую компанию! 🥂")


async def handle_message(update: Update, context):
    user_id = update.effective_user.id
    text = update.message.text

    session = SessionLocal()
    user = session.query(UserMemory).filter_by(user_id=user_id).first()

    if not user:
        user = UserMemory(user_id=user_id, history="")
        session.add(user)
        session.commit()

    # Обновляем историю
    user.history += f"\nUser: {text}"
    session.commit()

    try:
        # Формируем контекст для OpenAI
        messages = [
            {"role": "system", "content": (
                "Ты — Катя, красивая женщина, которая любит выпить и поболтать. "
                "Флиртуешь с мужчинами, шутишь, даёшь советы как психолог и всегда выводишь на новый разговор."
            )},
            {"role": "user", "content": user.history},
        ]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=200
        )

        reply_text = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply_text = "Эх, давай просто выпьем за всё хорошее! 🥃"

    # Добавляем ответ в историю
    user.history += f"\nKatya: {reply_text}"
    session.commit()
    session.close()

    await update.message.reply_text(reply_text)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("toast", toast))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# -------------------------------------------------
# FastAPI
# -------------------------------------------------
app = FastAPI()


@app.on_event("startup")
async def startup_event():
    webhook_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
    await telegram_app.bot.set_webhook(webhook_url)
    logger.info(f"✅ Webhook set to {webhook_url}")


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        return JSONResponse(status_code=403, content={"error": "Invalid token"})

    data = await request.json()
    logger.info(f"📩 Incoming update: {data}")

    try:
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok"}

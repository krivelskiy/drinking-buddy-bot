import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from openai import OpenAI
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import sessionmaker, declarative_base

# -----------------------------------
# Логирование
# -----------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("app")

# -----------------------------------
# Переменные окружения
# -----------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./memory.db")

# -----------------------------------
# Инициализация OpenAI
# -----------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("✅ OpenAI client initialized")

# -----------------------------------
# SQLAlchemy
# -----------------------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL.replace("aiosqlite", "sqlite"), echo=False, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class UserMemory(Base):
    __tablename__ = "user_memory"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, unique=True)
    name = Column(String(100))
    favorite_drink = Column(String(100))
    history = Column(Text)


Base.metadata.create_all(bind=engine)
logger.info("✅ Database initialized")

# -----------------------------------
# Telegram bot
# -----------------------------------
tapp = Application.builder().token(BOT_TOKEN).build()

# Стикеры
STICKERS = {
    "водка": "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "виски": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "вино": "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "пиво": "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
    "грусть": "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "веселье": "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
}


async def start(update: Update, context):
    user = update.effective_user
    name = user.first_name if user else "друг"
    text = "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)


async def handle_message(update: Update, context):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    session = SessionLocal()
    memory = session.query(UserMemory).filter_by(user_id=user_id).first()
    if not memory:
        memory = UserMemory(user_id=user_id, name=user_name, favorite_drink="", history="")
        session.add(memory)
        session.commit()

    user_text = update.message.text
    memory.history = (memory.history or "") + f"\nUser: {user_text}"

    # Сохраняем любимый напиток
    if "любимый" in user_text.lower() and "пиво" in user_text.lower():
        memory.favorite_drink = "пиво"
    elif "любимый" in user_text.lower() and "вино" in user_text.lower():
        memory.favorite_drink = "вино"
    elif "любимый" in user_text.lower() and "водка" in user_text.lower():
        memory.favorite_drink = "водка"
    elif "любимый" in user_text.lower() and "виски" in user_text.lower():
        memory.favorite_drink = "виски"

    session.commit()

    # Проверяем, нужно ли отправить стикер
    for key, sticker_id in STICKERS.items():
        if key in user_text.lower():
            await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=sticker_id)

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — Катя, собутыльница, любишь выпить, пошутить и флиртовать. "
                        "Ты помогаешь как психолог и всегда поддерживаешь разговор."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        )
        response_text = completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        response_text = "Эх, давай просто выпьем за всё хорошее! 🥃"

    memory.history += f"\nBot: {response_text}"
    session.commit()
    session.close()

    await context.bot.send_message(chat_id=update.effective_chat.id, text=response_text)


tapp.add_handler(CommandHandler("start", start))
tapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# -----------------------------------
# FastAPI
# -----------------------------------
app = FastAPI()


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        return JSONResponse(status_code=403, content={"error": "Forbidden"})

    try:
        data = await request.json()
        update = Update.de_json(data)  # ✅ фикс: убрали параметр context
        await tapp.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    return JSONResponse(content={"ok": True})


@app.get("/")
async def root():
    return {"status": "ok"}

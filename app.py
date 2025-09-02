import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from openai import OpenAI

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загружаем переменные
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Инициализация бота
application = Application.builder().token(BOT_TOKEN).build()
client = OpenAI(api_key=OPENAI_API_KEY)

# FastAPI
app = FastAPI()


# --- Handlers ---
async def start(update: Update, context):
    await update.message.reply_text("Привет! Я твой собутыльник 🤝 Напиши что-нибудь!")


async def toast(update: Update, context):
    toasts = [
        "За здоровье! 🥂",
        "За дружбу! 🍻",
        "За удачу! 🍷",
        "Чтобы утром не болеть! 🍺",
    ]
    import random
    await update.message.reply_text(random.choice(toasts))


async def chat(update: Update, context):
    user_message = update.message.text
    logger.info(f"User said: {user_message}")

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты собутыльник. Разговаривай дружески, шути, предлагай тосты."},
                {"role": "user", "content": user_message},
            ],
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "Эх, давай просто выпьем за всё хорошее! 🥃"

    await update.message.reply_text(reply)


# Регистрируем команды
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("toast", toast))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))


# --- Webhook ---
@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(request: Request):
    data = await request.json()
    logger.info(f"Incoming update: {data}")
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"status": "ok"}

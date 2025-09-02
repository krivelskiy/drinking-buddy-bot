import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# OpenAI клиент
client = OpenAI(api_key=OPENAI_API_KEY)

# Telegram приложение
application = Application.builder().token(BOT_TOKEN).build()

# Фраза по умолчанию
FALLBACK_PHRASE = "Ну что, дружище, давай выпьем за здоровье! 🥂"


# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я твой собутыльник 🤝 Напиши что-нибудь или попроси тост через /toast")


# /toast
async def toast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты весёлый собутыльник, придумываешь короткие тосты."},
                {"role": "user", "content": "Придумай тост."}
            ]
        )
        toast_text = response.choices[0].message.content if response and response.choices else None
        await update.message.reply_text(toast_text or FALLBACK_PHRASE)
    except Exception as e:
        logger.error(f"❌ Ошибка OpenAI в /toast: {e}")
        await update.message.reply_text(FALLBACK_PHRASE)


# Ответ на любые сообщения
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты собутыльник: поддерживаешь разговор, шутишь, предлагаешь тосты."},
                {"role": "user", "content": update.message.text}
            ]
        )
        reply = response.choices[0].message.content if response and response.choices else None
        await update.message.reply_text(reply or FALLBACK_PHRASE)
    except Exception as e:
        logger.error(f"❌ Ошибка OpenAI в chat: {e}")
        await update.message.reply_text(FALLBACK_PHRASE)


# Роуты
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("toast", toast))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))


# Для Render
import asyncio
from fastapi import FastAPI
from telegram.ext import Updater

web_app = FastAPI()

@web_app.get("/")
async def root():
    return {"status": "ok"}

async def run_bot():
    await application.initialize()
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        url_path=BOT_TOKEN,
        webhook_url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{BOT_TOKEN}"
    )

asyncio.get_event_loop().create_task(run_bot())

app = web_app

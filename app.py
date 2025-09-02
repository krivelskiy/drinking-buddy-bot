import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)
from openai import OpenAI

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Переменные ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY is not set")

# --- OpenAI клиент ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Telegram Application ---
application = Application.builder().token(BOT_TOKEN).build()

# --- FastAPI ---
app = FastAPI()

# --- Команды ---
async def start(update: Update, context):
    await update.message.reply_text("🍻 Привет! Я твой виртуальный собутыльник. Напиши что-нибудь или попроси тост!")

async def toast(update: Update, context):
    prompt = "Придумай короткий тост для дружеской компании за столом."
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.choices[0].message.content
    await update.message.reply_text(text)

# --- Ответ на любое сообщение ---
async def chat(update: Update, context):
    user_text = update.message.text
    prompt = f"Ты виртуальный собутыльник. Пользователь написал: {user_text}. Ответь дружелюбно, как друг за столом."
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.choices[0].message.content
    await update.message.reply_text(text)

# --- Регистрация хэндлеров ---
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("toast", toast))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

# --- Webhook обработчик ---
@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook_handler(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"status": "ok"}

# --- Healthcheck ---
@app.get("/")
async def root():
    return {"status": "ok"}

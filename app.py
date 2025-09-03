import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from openai import OpenAI

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

telegram_app = Application.builder().token(BOT_TOKEN).build()

# --- Handlers ---
async def start(update: Update, context):
    await update.message.reply_text(
        "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
    )

async def toast(update: Update, context):
    await update.message.reply_text("Эх, давай просто выпьем за всё хорошее! 🥃")

async def chat_with_katya(update: Update, context):
    user_message = update.message.text
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "Ты — Катя, красивая женщина. "
                    "Ты любишь выпить и поболтать. "
                    "Ты флиртуешь с мужчинами, шутишь, даёшь советы как психолог "
                    "и всегда стараешься поддерживать диалог, задавая новые вопросы."
                )},
                {"role": "user", "content": user_message},
            ],
            max_tokens=200,
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "Эх, давай просто выпьем за всё хорошее! 🥃"

    await update.message.reply_text(reply)

# --- Register handlers ---
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("toast", toast))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_katya))

# --- FastAPI webhook ---
@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return JSONResponse(content={"ok": True})

@app.get("/")
async def root():
    return {"status": "ok"}

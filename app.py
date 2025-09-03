import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from openai import OpenAI

# --- Логи ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("app")

# --- ENV ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not APP_BASE_URL:
    raise RuntimeError("APP_BASE_URL is not set")

# --- OpenAI ---
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("OpenAI client is initialized.")

# --- Telegram Application ---
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --- Handlers ---
async def start(update: Update, _):
    await update.message.reply_text("Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?")

async def toast(update: Update, _):
    await update.message.reply_text("Эх, давай просто выпьем за всё хорошее! 🥃")

SYSTEM_PROMPT = (
    "Ты — Катя, красивая женщина. Ты любишь выпить и поболтать. "
    "Ты флиртуешь с мужчинами, шутишь, даёшь мягкие советы как психолог "
    "и всегда поддерживаешь диалог, задавая новые вопросы. Отвечай дружелюбно,"
    " коротко и живо, без занудства."
)

async def chat_with_katya(update: Update, _):
    user_message = update.message.text or ""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=220,
            temperature=0.9,
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not reply:
            reply = "Эх, давай просто выпьем за всё хорошее! 🥃"
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "Эх, давай просто выпьем за всё хорошее! 🥃"

    await update.message.reply_text(reply)

# Регистрируем handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("toast", toast))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_katya))

# --- FastAPI app ---
app = FastAPI()

# Инициализация/деинициализация PTB + установка вебхука
@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    webhook_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
    await telegram_app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook set to: {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await telegram_app.bot.delete_webhook()
    except Exception as e:
        logger.warning(f"delete_webhook error: {e}")
    await telegram_app.shutdown()
    await telegram_app.stop()

# Точка приёма апдейтов от Telegram
@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        # Отвечаем 200, чтобы Telegram не ретраил бесконечно
    return JSONResponse({"ok": True})

@app.get("/")
async def root():
    return {"status": "ok"}

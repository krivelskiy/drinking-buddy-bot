import os
import logging
import asyncio
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------- ЛОГИ ---------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("app")

# --------- ENV ---------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not BOT_TOKEN:
    log.warning("ENV BOT_TOKEN is empty!")
if not OPENAI_API_KEY:
    log.warning("ENV OPENAI_API_KEY is empty! Bot will use fallback replies.")

# Public URL на Render доступен как RENDER_EXTERNAL_URL
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
if not PUBLIC_URL:
    # не критично: вебхук можно выставить вручную; просто логируем
    log.warning("PUBLIC_URL/RENDER_EXTERNAL_URL is not set. Webhook auto-set will be skipped.")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL else None

# --------- OpenAI client (v1.x) ---------
reply_fallback = "Эх, давай просто выпьем за всё хорошее! 🥃"

client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client is initialized.")
    except Exception as e:
        log.error(f"OpenAI init failed: {e}")
        client = None


async def generate_reply(prompt: str) -> str:
    """
    Вызов OpenAI (с ограничениями) с падением в фолбэк.
    Используем поток, т.к. SDK синхронный.
    """
    if not client:
        return reply_fallback

    system_prompt = (
        "Ты — дружелюбный собутыльник. Отвечай коротко, по-русски, с атмосферой бара: "
        "чуть шуток, теплоты, эмодзи по вкусу. Избегай токсичности и грубостей."
    )

    def _call():
        # Используем Chat Completions для стабильности
        return client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=180,
            temperature=0.7,
        )

    try:
        resp = await asyncio.to_thread(_call)
        text = resp.choices[0].message.content.strip()
        return text or reply_fallback
    except Exception as e:
        log.error(f"OpenAI error: {e}")
        return reply_fallback


# --------- Telegram handlers ---------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой собутыльник 🍻 Пиши что угодно — поддержу разговор. "
        "Команда /toast — поднимем бокалы!"
    )


async def cmd_toast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random
    toasts = [
        "За здоровье! 🥂",
        "За дружбу и удачу! 🍻",
        "Чтобы утро было добрым! 🍺",
        "За тех, кто с нами! 🥃",
        "За мечты, которые сбудутся! 🍷",
    ]
    await update.message.reply_text(random.choice(toasts))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""
    reply = await generate_reply(user_text)
    await update.message.reply_text(reply)


# --------- FastAPI + PTB lifecycle ---------
app = FastAPI()

# Создаем Telegram Application (без polling)
application: Optional[Application] = None
if BOT_TOKEN:
    application = Application.builder().token(BOT_TOKEN).build()
    # Регистрируем хэндлеры сразу
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("toast", cmd_toast))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
else:
    log.error("BOT_TOKEN is missing — Telegram part will not start.")


@app.get("/")
def root():
    return {"status": "ok"}


@app.on_event("startup")
async def _startup():
    if not application:
        return
    log.info("Starting Telegram application...")
    await application.initialize()
    await application.start()
    # Вебхук выставляем автоматически, если известен публичный URL
    if WEBHOOK_URL:
        try:
            await application.bot.set_webhook(WEBHOOK_URL, allowed_updates=["message"])
            log.info(f"Webhook set to: {WEBHOOK_URL}")
        except Exception as e:
            log.error(f"Failed to set webhook: {e}")
    else:
        log.warning("Webhook auto-set skipped (no PUBLIC_URL/RENDER_EXTERNAL_URL).")


@app.on_event("shutdown")
async def _shutdown():
    if not application:
        return
    await application.stop()
    await application.shutdown()


# Telegram webhook endpoint — ДОЛЖЕН совпадать с setWebhook
@app.post(WEBHOOK_PATH if BOT_TOKEN else "/webhook-not-configured")
async def telegram_webhook(request: Request):
    """
    Telegram шлет апдейты сюда. Мы передаем их в PTB.
    Возвращаем 200 OK всегда, чтобы Telegram не ретраил.
    """
    try:
        data = await request.json()
        log.info(f"Incoming update: {data}")
        if not application:
            return JSONResponse({"ok": True})

        update = Update.de_json(data, application.bot)
        # передаём апдейт в PTB
        await application.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        log.error(f"webhook error: {e}")
        # отвечаем 200, но текстом — чтобы Telegram не считал это ошибкой
        return PlainTextResponse("ok")

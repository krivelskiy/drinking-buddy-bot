import os
import re
import logging
from typing import Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes
from telegram.ext import CommandHandler, MessageHandler, filters

# === ТВОИ существующие импорты (OpenAI, SQLAlchemy, модели, и т.д.) остаются ===
# from openai import OpenAI
# ... и все, что уже было у тебя выше/ниже, не трогаем

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

BOT_TOKEN = os.environ["BOT_TOKEN"]
APP_BASE_URL = os.environ["APP_BASE_URL"]  # уже есть у тебя

app = FastAPI(title="drinking-buddy-bot")

# ---------------------------
# 1) Маппинг file_id стикеров
# ---------------------------
DRINK_STICKERS = {
    # напитки
    "beer": "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
    "wine": "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "whisky": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "vodka": "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    # эмоции (на всякий случай — можно использовать дальше)
    "happy": "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
    "sad":   "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
}

# Ключевые слова на RU/EN для классов напитков
DRINK_KEYWORDS = {
    "beer": [
        r"\bпиво\b", r"\bпивко\b", r"\bлагер\b", r"\bэ(й|i)ль\b", r"\bstout\b", r"\bipa\b",
        r"\bbeer\b", r"\bpilsner\b", r"\bбарнаульск(ое|ий)\b"
    ],
    "wine": [
        r"\bвино\b", r"\bкрасное\b вино", r"\bбелое\b вино", r"\bроз(е|э)\b", r"\bwine\b", r"\bшардоне\b", r"\бмерло\b"
    ],
    "whisky": [
        r"\bвис(ки|кии)\b", r"\bскотч\b", r"\bбурбон\b", r"\bwhisk(e?)y\b"
    ],
    "vodka": [
        r"\bводк[аи]\b", r"\bvodka\b", r"\bстопк(а|у|и)\b"
    ],
    # При желании легко добавить "champagne", "rum", "gin" и т.д.
}

# 2) Функция детекции напитка
def detect_drink(text: str) -> Optional[Tuple[str, str]]:
    """Возвращает ('beer'|'wine'|'whisky'|'vodka', matched_word) либо None."""
    t = text.lower()
    for drink, patterns in DRINK_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, t):
                return drink, pat
    return None

# === Ниже оставь твои текущие объекты БД, OpenAI-клиент, prepare_prompt и т.п. ===
# client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
# SessionLocal, Base, User, Message ... и т.д.

# ---------------------------
# 3) Telegram application
# ---------------------------
tapp: Application = ApplicationBuilder().token(BOT_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?")

async def toast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("За всё хорошее! 🥂")

# Главное: обработчик обычного текста
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text_in = update.message.text

    # 3.1 детектируем напиток заранее (для стикера и для подсказки в промпт)
    drink_detection = detect_drink(text_in)
    if drink_detection:
        drink_key, matched = drink_detection
        log.info(f"[drink] user={user_id} matched={matched} -> {drink_key}")
    else:
        log.info(f"[drink] user={user_id} matched=None")

    # 3.2 сохраняем вход в БД, подтягиваем память — ТВОЙ текущий код здесь
    # save_message(user_id, 'user', text_in)  # пример

    # 3.3 генерим ответ Каті (ТВОЙ текущий вызов OpenAI)
    # reply_text = await generate_reply(user_id, text_in, drink_hint=drink_detection[0] if drink_detection else None)
    # На случай проблем с OpenAI/БД — безопасный ответ:
    reply_text = None
    try:
        # Замени на твою функцию генерации:
        # reply_text = await gen_with_openai(user_id, text_in, drink_detection[0] if drink_detection else None)
        pass
    except Exception as e:
        log.exception("OpenAI generation failed")
    if not reply_text:
        reply_text = "Давай просто выпьем за всё хорошее! 🥂 Что нальём?"

    # 3.4 шлём текст
    await context.bot.sendMessage(chat_id=chat_id, text=reply_text)

    # 3.5 если распознали напиток — шлём подходящий стикер
    if drink_detection:
        drink_key, _ = drink_detection
        file_id = DRINK_STICKERS.get(drink_key)
        if file_id:
            try:
                await context.bot.sendSticker(chat_id=chat_id, sticker=file_id)
                log.info(f"[sticker] sent {drink_key} to user={user_id}")
            except Exception:
                log.exception("[sticker] failed to send")

    # 3.6 сохраняем ответ в БД — ТВОЙ текущий код
    # save_message(user_id, 'assistant', reply_text)

# Зарегистрируем хендлеры
tapp.add_handler(CommandHandler("start", start_cmd))
tapp.add_handler(CommandHandler("toast", toast_cmd))
tapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

# ---------------------------
# 4) FastAPI endpoints
# ---------------------------
@app.get("/")
async def health():
    return PlainTextResponse("OK")

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    update = Update.de_json(await request.json(), context=tapp.bot)
    try:
        await tapp.initialize()  # важно для корректной обработки апдейта
        await tapp.process_update(update)
    except Exception:
        log.exception("Webhook error")
    return JSONResponse({"ok": True})

# Установка вебхука на старте
@app.on_event("startup")
async def on_startup():
    webhook_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
    try:
        await tapp.bot.set_webhook(webhook_url)
        log.info(f"✅ Webhook set to {webhook_url}")
    except Exception:
        log.exception("Failed to set webhook")

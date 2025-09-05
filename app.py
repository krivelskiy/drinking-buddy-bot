import os
import httpx
import re
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from sqlalchemy import create_engine, text, DDL
from sqlalchemy.engine import Engine

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from openai import OpenAI

from config import DATABASE_URL, OPENAI_API_KEY, BOT_TOKEN, RENDER_EXTERNAL_URL
from constants import (
    STICKERS, DRINK_KEYWORDS, DB_FIELDS, FALLBACK_OPENAI_UNAVAILABLE,
    USERS_TABLE, MESSAGES_TABLE, BEER_STICKERS, STICKER_TRIGGERS
)

# -----------------------------
# Логирование
# -----------------------------
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# -----------------------------
# Инициализация БД
# -----------------------------
engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
logger.info("✅ Database engine created")

# -----------------------------
# OpenAI клиент
# -----------------------------
client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
    except Exception as e:
        logger.exception("OpenAI init failed: %s", e)
else:
    logger.warning("OPENAI_API_KEY is empty — ответы будут с заглушкой")

# -----------------------------
# Telegram Application
# -----------------------------
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is empty (webhook/бот работать не будет)")

tapp: Optional[Application] = None

def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app_ = Application.builder().token(BOT_TOKEN).build()

    app_.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    return app_

# -----------------------------
# Утилиты БД
# -----------------------------
U = DB_FIELDS["users"]  # короткий алиас

def upsert_user_from_update(update: Update) -> None:
    """
    Без ON CONFLICT, чтобы не зависеть от уникальных ограничений:
    1) ищем пользователя по chat_id или tg_id
    2) UPDATE если найден, иначе INSERT
    """
    if update.effective_chat is None or update.effective_user is None:
        return

    chat_id = update.effective_chat.id
    tg_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    last_name = update.effective_user.last_name

    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {U['pk']} FROM users WHERE {U['pk']} = :chat_id OR {U['tg_id']} = :tg_id"),
            {"chat_id": chat_id, "tg_id": tg_id},
        ).fetchone()

        if row:
            conn.execute(
                text(f"""
                    UPDATE users
                    SET {U['tg_id']} = :tg_id,
                        {U['username']} = :username,
                        {U['first_name']} = :first_name,
                        {U['last_name']} = :last_name,
                        {U['updated_at']} = now()
                    WHERE {U['pk']} = :chat_id OR {U['tg_id']} = :tg_id
                """),
                {
                    "chat_id": chat_id,
                    "tg_id": tg_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )
        else:
            conn.execute(
                text(f"""
                    INSERT INTO users ({U['pk']}, {U['tg_id']}, {U['username']}, {U['first_name']}, {U['last_name']})
                    VALUES (:chat_id, :tg_id, :username, :first_name, :last_name)
                """),
                {
                    "chat_id": chat_id,
                    "tg_id": tg_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )

def load_user_summary(chat_id: int) -> Optional[str]:
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {U['summary']} FROM users WHERE {U['pk']} = :chat_id"),
            {"chat_id": chat_id},
        ).fetchone()
        return row[0] if row and row[0] else None

def save_user_summary(chat_id: int, summary: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE users
                SET {U['summary']} = :summary, {U['updated_at']} = now()
                WHERE {U['pk']} = :chat_id
            """),
            {"chat_id": chat_id, "summary": summary},
        )

# -----------------------------
# Генерация ответа
# -----------------------------
SYSTEM_PROMPT = (
    "Ты — Катя Собутыльница: дружелюбная, немного флиртуешь, любишь пошутить. "
    "Отвечай коротко и по делу. Если пользователь сообщает факт о себе (возраст, имя, любимые напитки), "
    "можешь подтвердить и не забывай про атмосферу бара."
)

async def llm_reply(user_text: str, username: Optional[str], summary: Optional[str]) -> str:
    if client is None:
        return FALLBACK_OPENAI_UNAVAILABLE
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        if summary:
            messages.append({"role": "system", "content": f"Память о пользователе: {summary}"})
        if username:
            messages.append({"role": "system", "content": f"Username пользователя: @{username}"})
        messages.append({"role": "user", "content": user_text})

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return FALLBACK_OPENAI_UNAVAILABLE

# -----------------------------
# Хендлер сообщений
# -----------------------------
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Incoming update_id=%s", getattr(update, "update_id", "n/a"))

    if update.message is None or update.message.text is None:
        return

    # 1) апсерт пользователя
    try:
        upsert_user_from_update(update)
    except Exception:
        logger.exception("Failed to upsert user — продолжим без записи")

    chat_id = update.effective_chat.id  # type: ignore
    username = update.effective_user.username if update.effective_user else None  # type: ignore
    text_in = update.message.text

    # 2) память пользователя из БД
    summary = None
    try:
        summary = load_user_summary(chat_id)
    except Exception:
        logger.exception("Failed to load user summary")

    # 3) ответ через OpenAI (или заглушка)
    answer = await llm_reply(text_in, username, summary)

    await update.message.reply_text(answer)

    # 4) если упомянут напиток — отправляем соответствующий стикер
    lower = text_in.lower()
    for kw, sticker_key in DRINK_KEYWORDS.items():
        if kw in lower:
            try:
                await context.bot.send_sticker(chat_id=chat_id, sticker=STICKERS[sticker_key])
            except Exception:
                logger.exception("Failed to send sticker for %s", kw)
            break

# -----------------------------
# FastAPI приложение
# -----------------------------
app = FastAPI()

class TelegramUpdate(BaseModel):
    update_id: int | None = None  # не обязателен, структура свободная — прокидываем сырой JSON

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

@app.post(f"/webhook/{{token}}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.de_json(data, tapp.bot)  # type: ignore
    await tapp.process_update(update)        # type: ignore
    return PlainTextResponse("OK")

# -----------------------------
# События запуска/остановки
# -----------------------------
@app.on_event("startup")
async def on_startup():
    global tapp
    tapp = build_application()
    await tapp.initialize()
    await tapp.start()

    # Ставим вебхук, если можем вычислить внешний URL
    base_url = os.getenv("RENDER_EXTERNAL_URL")
    if base_url:
        try:
            await tapp.bot.set_webhook(f"{base_url}/webhook/{BOT_TOKEN}")
            logger.info("✅ Webhook set to %s/webhook/%s", base_url, BOT_TOKEN)
        except Exception:
            logger.exception("Failed to set webhook")
    else:
        logger.warning("RENDER_EXTERNAL_URL is empty — webhook не установлен")

@app.on_event("shutdown")
async def on_shutdown():
    if tapp:
        try:
            await tapp.stop()
        except Exception:
            logger.exception("Error on telegram app stop")

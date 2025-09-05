import asyncio
import json
import logging
import os
import re
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

from openai import OpenAI, APIError, APITimeoutError

# Конфиг и константы — единая «точка правды»
from config import BOT_TOKEN, OPENAI_API_KEY, DATABASE_URL, BASE_URL
from constants import STICKERS, DRINK_KEYWORDS, DB_FIELDS, FALLBACK_OPENAI_UNAVAILABLE

# ---------------------- Логирование ----------------------
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# ---------------------- FastAPI app ----------------------
app = FastAPI(title="Drinking Buddy Bot")

# ---------------------- DB ----------------------
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

USERS = DB_FIELDS["users"]  # короткий алиас для полей

def upsert_user_from_update(update: Update) -> None:
    """Сохраняем/обновляем базовые поля пользователя из апдейта (username/first/last/tg_id)."""
    if update.effective_user is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user

    with engine.begin() as conn:
        # upsert по pk = chat_id
        conn.execute(
            text(
                f"""
                INSERT INTO users ({USERS["pk"]}, {USERS["tg_id"]}, {USERS["username"]}, {USERS["first_name"]}, {USERS["last_name"]})
                VALUES (:chat_id, :tg_id, :username, :first_name, :last_name)
                ON CONFLICT ({USERS["pk"]}) DO UPDATE SET
                    {USERS["tg_id"]} = EXCLUDED.{USERS["tg_id"]},
                    {USERS["username"]} = EXCLUDED.{USERS["username"]},
                    {USERS["first_name"]} = EXCLUDED.{USERS["first_name"]},
                    {USERS["last_name"]} = EXCLUDED.{USERS["last_name"]},
                    {USERS["updated_at"]} = now()
                """
            ),
            {
                "chat_id": chat_id,
                "tg_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        )

def set_user_name(chat_id: int, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                UPDATE users
                SET {USERS["name"]} = :name, {USERS["updated_at"]} = now()
                WHERE {USERS["pk"]} = :chat_id
                """
            ),
            {"chat_id": chat_id, "name": name},
        )

def append_summary_fact(chat_id: int, fact: str) -> None:
    """Храним краткие факты в текстовом поле summary (каждый факт на новой строке)."""
    with engine.begin() as conn:
        # Прочитать текущий summary
        cur = conn.execute(
            text(f"SELECT {USERS['summary']} FROM users WHERE {USERS['pk']} = :chat_id"),
            {"chat_id": chat_id},
        ).first()
        summary = (cur[0] or "").strip() if cur else ""
        if summary:
            summary = summary + "\n" + fact
        else:
            summary = fact
        conn.execute(
            text(
                f"""
                UPDATE users
                SET {USERS["summary"]} = :summary, {USERS["updated_at"]} = now()
                WHERE {USERS["pk"]} = :chat_id
                """
            ),
            {"chat_id": chat_id, "summary": summary},
        )

def get_user_summary(chat_id: int) -> str:
    with engine.begin() as conn:
        cur = conn.execute(
            text(f"SELECT {USERS['summary']} FROM users WHERE {USERS['pk']} = :chat_id"),
            {"chat_id": chat_id},
        ).first()
        return (cur[0] or "").strip() if cur else ""

# ---------------------- OpenAI ----------------------
client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
    except Exception as e:
        logger.warning("⚠️ OpenAI init failed: %s", e)
        client = None

async def ask_openai(history: list[dict]) -> Optional[str]:
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history,
            temperature=0.6,
        )
        return resp.choices[0].message.content
    except (APIError, APITimeoutError, Exception) as e:
        logger.error("OpenAI error: %s", e)
        return None

# ---------------------- Telegram (PTB) ----------------------
tapp: Optional[Application] = None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    upsert_user_from_update(update)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Привет! Я Катя Собутыльница 🍻")

DRINK_REGEX = re.compile("|".join(map(re.escape, DRINK_KEYWORDS.keys())), re.IGNORECASE)
AGE_REGEX = re.compile(r"\bмне\s+(\d+)\s+лет\b", re.IGNORECASE)
NAME_REGEX = re.compile(r"\bменя\s+зовут\s+([A-Za-zА-Яа-яЁё\- ]{2,40})\b", re.IGNORECASE)

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return

    upsert_user_from_update(update)
    chat_id = update.effective_chat.id
    text_msg = (update.message.text or "").strip()

    # 1) Разобрать факты и сохранить
    m_age = AGE_REGEX.search(text_msg)
    if m_age:
        age = m_age.group(1)
        append_summary_fact(chat_id, f"Возраст: {age}")
        # не выходим — продолжаем диалог через OpenAI

    m_name = NAME_REGEX.search(text_msg)
    if m_name:
        name = m_name.group(1).strip()
        set_user_name(chat_id, name)
        append_summary_fact(chat_id, f"Имя: {name}")

    # 2) Стикер по напитку (параллельно диалогу)
    m_drink = DRINK_REGEX.search(text_msg)
    if m_drink:
        key = m_drink.group(0).lower()
        mapping_key = DRINK_KEYWORDS.get(key)
        if mapping_key:
            sticker_id = STICKERS[mapping_key]
            try:
                await context.bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            except Exception as e:
                logger.warning("Failed to send sticker: %s", e)

    # 3) Диалог — строго через OpenAI. Если недоступен, даём заглушку и выходим.
    summary = get_user_summary(chat_id)
    history = [
        {"role": "system", "content": "Ты — Катя Собутыльница. Отвечай дружелюбно и коротко."},
    ]
    if summary:
        history.append({"role": "system", "content": f"Известные факты о пользователе:\n{summary}"})
    history.append({"role": "user", "content": text_msg})

    answer = await ask_openai(history)
    if answer is None:
        await context.bot.send_message(chat_id=chat_id, text=FALLBACK_OPENAI_UNAVAILABLE)
        return

    await context.bot.send_message(chat_id=chat_id, text=answer)

# ---------------------- Инициализация бота ----------------------
def build_telegram_app() -> Optional[Application]:
    if not BOT_TOKEN:
        logger.warning("⚠️ BOT_TOKEN пуст — вебхук работать не будет")
        return None
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    return application

async def set_webhook_if_possible(app_: Application) -> None:
    """Ставим вебхук, если известен BASE_URL и BOT_TOKEN."""
    if not (BASE_URL and BOT_TOKEN):
        logger.warning("⚠️ BASE_URL или BOT_TOKEN не заданы — пропускаю setWebhook")
        return
    url = f"{BASE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    try:
        await app_.bot.set_webhook(url)
        logger.info("✅ Webhook set to %s", url)
    except Exception as e:
        logger.error("Failed to set webhook: %s", e)

# ---------------------- FastAPI lifecycle ----------------------
@app.on_event("startup")
async def on_startup():
    global tapp
    # Пробуем коннект к БД (создание пула)
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ Database initialized")
    except Exception as e:
        logger.error("DB init failed: %s", e)
        # не падаем — сервис поднимется хотя бы для /health

    tapp = build_telegram_app()
    if tapp:
        await tapp.initialize()
        await set_webhook_if_possible(tapp)
        logger.info("✅ Telegram Application initialized")

@app.on_event("shutdown")
async def on_shutdown():
    if tapp:
        try:
            await tapp.shutdown()
        except Exception:
            pass

# ---------------------- HTTP endpoints ----------------------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    ok_db = True
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        ok_db = False
    return {"ok": True, "db": ok_db, "webhook": bool(tapp)}

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if not tapp or not BOT_TOKEN or token != BOT_TOKEN:
        # возвращаем 403, чтобы телега не считала доставленным
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    logger.info("Incoming update_id=%s", data.get("update_id"))

    update = Update.de_json(data=data, bot=tapp.bot)
    await tapp.process_update(update)
    return Response(status_code=200)

# ---------------------- Служебный эндпоинт: структура таблицы ----------------------
class TableInfo(BaseModel):
    table: str
    columns: list[dict]

@app.get("/debug/db/users")
async def debug_users_table() -> TableInfo:
    """Возвращает структуру таблицы users из 'единоисточника' constants.DB_FIELDS."""
    cols = [
        {"name": USERS["pk"], "type": "BIGINT", "nullable": False},
        {"name": USERS["tg_id"], "type": "BIGINT", "nullable": False},
        {"name": USERS["username"], "type": "VARCHAR(255)", "nullable": True},
        {"name": USERS["first_name"], "type": "VARCHAR(255)", "nullable": True},
        {"name": USERS["last_name"], "type": "VARCHAR(255)", "nullable": True},
        {"name": USERS["name"], "type": "TEXT", "nullable": True},
        {"name": USERS["favorite_drinks"], "type": "JSONB", "nullable": True},
        {"name": USERS["summary"], "type": "TEXT", "nullable": True},
        {"name": USERS["free_drinks"], "type": "INTEGER", "nullable": False},
        {"name": USERS["created_at"], "type": "TIMESTAMP", "nullable": True},
        {"name": USERS["updated_at"], "type": "TIMESTAMP", "nullable": True},
    ]
    return TableInfo(table="users", columns=cols)

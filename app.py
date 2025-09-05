import json
import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import (
    create_engine, Column, BigInteger, String, Text, Integer, TIMESTAMP, inspect
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import config as cfg

# -----------------------------------------------------------------------------
# ЛОГИ
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("app")

# -----------------------------------------------------------------------------
# БАЗА ДАННЫХ
# -----------------------------------------------------------------------------
if not cfg.DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(cfg.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    # ВАЖНО: соответствует фактической схеме, которую ты прислал
    chat_id = Column(BigInteger, nullable=False)
    name = Column(Text, nullable=True)
    favorite_drinks = Column(JSONB, nullable=True)
    summary = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, nullable=True, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=True, default=datetime.utcnow)
    tg_id = Column(BigInteger, nullable=False, primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    free_drinks = Column(Integer, nullable=False, default=0)

# Ничего не создаём / не мигрируем — работаем с уже существующей таблицей
log.info("✅ Database initialized")

# -----------------------------------------------------------------------------
# СТИКЕРЫ
# -----------------------------------------------------------------------------
STICKERS = {
    "happy":  "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
    "sad":    "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "vodka":  "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "whisky": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "wine":   "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "beer":   "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
}

# -----------------------------------------------------------------------------
# TELEGRAM APPLICATION
# -----------------------------------------------------------------------------
def build_telegram_app() -> Optional[Application]:
    if not cfg.BOT_TOKEN:
        log.error("BOT_TOKEN is not set")
        return None
    return Application.builder().token(cfg.BOT_TOKEN).build()

tapp: Optional[Application] = build_telegram_app()

async def cmd_start(update: Update, _):
    await update.message.reply_text("Привет! Я Катя Собутыльница 🤖")

async def on_text(update: Update, _):
    """Простой режим без OpenAI: распознаём напитки + шлём стикер."""
    text = (update.message.text or "").lower()

    # апдейт/создание пользователя (сохраняем память)
    session = SessionLocal()
    try:
        uid = update.effective_user.id
        user = session.query(User).filter(User.tg_id == uid).first()
        if not user:
            user = User(
                tg_id=uid,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username,
                first_name=update.effective_user.first_name,
                last_name=update.effective_user.last_name,
            )
            session.add(user)
        else:
            # лёгкий апдейт полей, которые могли поменяться
            user.username = update.effective_user.username
            user.first_name = update.effective_user.first_name
            user.last_name = update.effective_user.last_name
        session.commit()
    finally:
        session.close()

    # распознаём напиток
    if "водк" in text:
        await update.message.reply_sticker(STICKERS["vodka"])
        await update.message.reply_text("Катя пьёт водку 🥃")
    elif "виск" in text:
        await update.message.reply_sticker(STICKERS["whisky"])
        await update.message.reply_text("Катя пьёт виски 🥃")
    elif "вин" in text and "вино" in text or ("красн" in text or "бел" in text):
        await update.message.reply_sticker(STICKERS["wine"])
        await update.message.reply_text("Катя пьёт вино 🍷")
    elif "пив" in text:
        await update.message.reply_sticker(STICKERS["beer"])
        await update.message.reply_text("Катя пьёт пиво 🍺")
    else:
        await update.message.reply_sticker(STICKERS["happy"])
        await update.message.reply_text("Катя рада с тобой выпить 😄")

if tapp:
    tapp.add_handler(CommandHandler("start", cmd_start))
    tapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

# -----------------------------------------------------------------------------
# FASTAPI
# -----------------------------------------------------------------------------
app = FastAPI(title="Drinking Buddy Bot")

@app.on_event("startup")
async def on_startup():
    # Инициализируем Telegram Application один раз
    if tapp:
        await tapp.initialize()

    # Ставим вебхук, если разрешено и есть базовый URL
    if tapp and cfg.AUTO_SET_WEBHOOK and cfg.APP_BASE_URL and cfg.BOT_TOKEN:
        url = f"{cfg.APP_BASE_URL}/webhook/{cfg.BOT_TOKEN}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"https://api.telegram.org/bot{cfg.BOT_TOKEN}/setWebhook",
                                  json={"url": url})
            if r.status_code == 200 and r.json().get("ok"):
                log.info(f"✅ Webhook set to {url}")
            else:
                log.warning(f"⚠️ setWebhook failed: {r.status_code} {r.text}")

@app.on_event("shutdown")
async def on_shutdown():
    if tapp:
        await tapp.shutdown()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/debug/schema")
def debug_schema():
    """Отдаём структуру таблицы users (без psql)."""
    insp = inspect(engine)
    cols = []
    for c in insp.get_columns("users"):
        cols.append({
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": c["nullable"],
            "default": str(c.get("default")),
        })
    return {"users": cols}

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    # защищаем хук: принимаем только правильный токен в пути
    if not cfg.BOT_TOKEN or token != cfg.BOT_TOKEN or not tapp:
        return JSONResponse(status_code=403, content={"ok": False})
    data = await request.json()
    update = Update.de_json(data, tapp.bot)
    await tapp.process_update(update)
    return {"ok": True}

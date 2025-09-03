import os
import re
import time
import random
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    ContextTypes,
    filters,
)

from openai import OpenAI

from sqlalchemy import create_engine, Column, Integer, String, Text, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base

# ---------------------------
# ЛОГИРОВАНИЕ
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("app")

# ---------------------------
# ОКРУЖЕНИЕ
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./memory.db")

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN is missing")
if not OPENAI_API_KEY:
    logger.error("❌ OPENAI_API_KEY is missing")
if not APP_BASE_URL:
    logger.warning("⚠️ APP_BASE_URL is missing (авто-установка вебхука будет пропущена)")

# ---------------------------
# OpenAI
# ---------------------------
client: Optional[OpenAI] = None
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("✅ OpenAI client initialized")
except Exception as e:
    logger.exception("❌ OpenAI init failed: %s", e)

# ---------------------------
# БАЗА (SQLAlchemy)
# ---------------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class UserMemory(Base):
    __tablename__ = "user_memory"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, unique=True)
    name = Column(String(100))
    favorite_drink = Column(String(100))
    history = Column(Text)

def ensure_schema():
    try:
        Base.metadata.create_all(bind=engine)
        insp = inspect(engine)

        if not insp.has_table("user_memory"):
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TABLE user_memory (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER UNIQUE,
                        name VARCHAR(100),
                        favorite_drink VARCHAR(100),
                        history TEXT
                    )
                """))
                logger.info("ℹ️ Created table user_memory manually")

        cols = {c["name"] for c in insp.get_columns("user_memory")}
        with engine.begin() as conn:
            if "favorite_drink" not in cols:
                conn.execute(text("ALTER TABLE user_memory ADD COLUMN favorite_drink VARCHAR(100)"))
                logger.info("🔧 Added column user_memory.favorite_drink")
            if "history" not in cols:
                conn.execute(text("ALTER TABLE user_memory ADD COLUMN history TEXT"))
                logger.info("🔧 Added column user_memory.history")
            if "name" not in cols:
                conn.execute(text("ALTER TABLE user_memory ADD COLUMN name VARCHAR(100)"))
                logger.info("🔧 Added column user_memory.name")
            if "user_id" not in cols:
                conn.execute(text("ALTER TABLE user_memory ADD COLUMN user_id INTEGER UNIQUE"))
                logger.info("🔧 Added column user_memory.user_id")
    except Exception as e:
        logger.exception("❌ ensure_schema failed: %s", e)
        raise

try:
    ensure_schema()
    logger.info("✅ Database initialized")
except Exception as e:
    logger.exception("❌ Database init failed: %s", e)

# ---------------------------
# Telegram bot (PTB v20)
# ---------------------------
tapp = Application.builder().token(BOT_TOKEN).build()

# Стикеры (file_id)
STICKER_ID = {
    "vodka": "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "whisky": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "wine": "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "beer": "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
    "sad": "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "happy": "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
}

# Регулярки → ключ
STICKER_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bпив(о|а|е|у|ом)?\b", re.IGNORECASE), "beer"),
    (re.compile(r"\bbeer\b", re.IGNORECASE), "beer"),
    (re.compile(r"\bвин(о|а|е|у|ом|ца)?\b", re.IGNORECASE), "wine"),
    (re.compile(r"\bwine\b", re.IGNORECASE), "wine"),
    (re.compile(r"\bводк(а|и|е|у|ой)?\b", re.IGNORECASE), "vodka"),
    (re.compile(r"\bvodka\b", re.IGNORECASE), "vodka"),
    (re.compile(r"\bвиск(и|аря|арю)?\b", re.IGNORECASE), "whisky"),
    (re.compile(r"\bwhisk(e|)y\b", re.IGNORECASE), "whisky"),
    (re.compile(r"\b(грустн|печаль|тоск)\w*\b", re.IGNORECASE), "sad"),
    (re.compile(r"\b(весел|радост|кайф)\w*\b", re.IGNORECASE), "happy"),
]

_last_sticker_ts: dict[int, float] = {}
STICKER_COOLDOWN_SEC = 5.0

# ---------------------------
# Хелперы памяти
# ---------------------------
def _save_history(session, user_id: int, user_name: str, user_text: str, bot_text: str | None = None):
    mem = session.query(UserMemory).filter_by(user_id=user_id).first()
    if not mem:
        mem = UserMemory(user_id=user_id, name=user_name, favorite_drink="", history="")
        session.add(mem)
        session.flush()
    if user_text:
        mem.history = (mem.history or "") + f"\nUser: {user_text}"
    if bot_text:
        mem.history = (mem.history or "") + f"\nBot: {bot_text}"
    session.commit()
    return mem

def _maybe_extract_favorite_drink(mem: UserMemory, text_in: str) -> None:
    low = (text_in or "").lower()
    for key, label in [
        ("пив", "пиво"), ("вин", "вино"), ("водк", "водка"), ("виск", "виски"),
        ("beer", "пиво"), ("wine", "вино"), ("vodka", "водка"), ("whisky", "виски"), ("whiskey", "виски"),
    ]:
        if key in low:
            mem.favorite_drink = label
            break

def pick_drink_sticker_by_name(name: str) -> str | None:
    mapping = {"пиво": "beer", "вино": "wine", "водка": "vodka", "виски": "whisky"}
    key = mapping.get((name or "").lower().strip())
    return STICKER_ID.get(key) if key else None

def maybe_send_sticker_for_text(text_in: str) -> str | None:
    for rx, key in STICKER_RULES:
        if rx.search(text_in or ""):
            return STICKER_ID.get(key)
    return None

# ---------------------------
# Хендлеры
# ---------------------------
async def start(update: Update, context: CallbackContext):
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?")

async def handle_message(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    user_name = user.first_name
    user_text = update.message.text or ""
    user_text_lower = user_text.lower()

    now = time.time()
    last_ts = _last_sticker_ts.get(chat_id, 0)

    # sticker by user text
    sticker_to_send: Optional[str] = maybe_send_sticker_for_text(user_text_lower)

    # если пишет «пей/выпьем» → любимый напиток
    if not sticker_to_send and re.search(r"\b(пей|выпьем|наливай)\b", user_text_lower):
        session = SessionLocal()
        try:
            mem = session.query(UserMemory).filter_by(user_id=user_id).first()
            sticker_to_send = pick_drink_sticker_by_name(mem.favorite_drink) if mem and mem.favorite_drink else STICKER_ID["beer"]
        finally:
            session.close()

    if sticker_to_send and (now - last_ts) >= STICKER_COOLDOWN_SEC:
        await context.bot.send_sticker(chat_id=chat_id, sticker=sticker_to_send)
        _last_sticker_ts[chat_id] = now

    # ---- GPT ----
    session = SessionLocal()
    try:
        mem = _save_history(session, user_id, user_name, user_text)
        _maybe_extract_favorite_drink(mem, user_text)
        session.commit()

        short_history = (mem.history or "").splitlines()[-20:]
        system_prompt = (
            "Ты — Катя, собутыльница. Женский тон, лёгкий флирт, юмор, дружелюбие. "
            "Иногда сама инициируешь выпивание и предлагаешь тост."
        )
        messages = [{"role": "system", "content": system_prompt}]
        for line in short_history:
            if not line.strip():
                continue
            role = "user" if line.startswith("User:") else "assistant"
            content = line.split(": ", 1)[1] if ": " in line else line
            messages.append({"role": role, "content": content})

        response_text = "Эх, давай просто выпьем за всё хорошее! 🥃"
        if client:
            completion = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
            response_text = (completion.choices[0].message.content or "").strip()

        _save_history(session, user_id, user_name, "", response_text)
    finally:
        session.close()

    await context.bot.send_message(chat_id=chat_id, text=response_text)

    # ---- Катя сама предлагает выпить (20%) ----
    if random.random() < 0.2 and (time.time() - last_ts) >= STICKER_COOLDOWN_SEC:
        session = SessionLocal()
        fav_sticker = STICKER_ID["beer"]
        try:
            mem = session.query(UserMemory).filter_by(user_id=user_id).first()
            if mem and mem.favorite_drink:
                sid = pick_drink_sticker_by_name(mem.favorite_drink)
                if sid:
                    fav_sticker = sid
        finally:
            session.close()

        toast_text = random.choice([
            "Давай я первая подниму бокал! 🥂 За нас!",
            "Ну что, предлагаю тост: за хорошее настроение! 🍻",
            "Я налила! Поднимем бокалы и выпьем вместе! 🍷",
        ])
        await context.bot.send_message(chat_id=chat_id, text=toast_text)
        await context.bot.send_sticker(chat_id=chat_id, sticker=fav_sticker)
        _last_sticker_ts[chat_id] = time.time()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("PTB error_handler caught exception", exc_info=context.error)

tapp.add_handler(CommandHandler("start", start))
tapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
tapp.add_error_handler(error_handler)

# ---------------------------
# FastAPI
# ---------------------------
app = FastAPI()

@app.on_event("startup")
async def _startup():
    await tapp.initialize()
    if APP_BASE_URL:
        wh_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
        await tapp.bot.set_webhook(url=wh_url, allowed_updates=["message"])
        logger.info("✅ Webhook set to %s", wh_url)

@app.on_event("shutdown")
async def _shutdown():
    await tapp.shutdown()

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        return JSONResponse(status_code=403, content={"ok": False, "error": "Forbidden"})
    data = await request.json()
    update = Update.de_json(data, tapp.bot)
    await tapp.process_update(update)
    return JSONResponse(content={"ok": True})

@app.get("/")
async def health():
    return {"status": "ok"}

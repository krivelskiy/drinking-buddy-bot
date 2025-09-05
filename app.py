import os
import json
import logging
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters

from httpx import HTTPError

# ==== наши константы (единая точка правды) ====
from constants import STICKERS, DRINK_KEYWORDS, DB_FIELDS, FALLBACK_OPENAI_UNAVAILABLE

# ---------- ЛОГИ ----------
logger = logging.getLogger("app")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip()
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "true").strip().lower() in ("1", "true", "yes", "y")

if not DATABASE_URL:
    logger.warning("DATABASE_URL is empty — persistence will fail")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is empty — Telegram бот работать не будет")

if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY is empty — включится fallback-ответ (без диалога)")

# ---------- БД ----------
engine: Optional[Engine] = None

def init_db() -> Engine:
    global engine
    if engine:
        return engine
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    logger.info("✅ Database initialized")
    return engine

# простейший upsert пользователя (без требований к unique индексам)
async def upsert_user(update: Update) -> None:
    if not engine:
        return
    u_tbl = DB_FIELDS["users"]
    msg = update.effective_message
    user = update.effective_user

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None or user is None:
        return

    payload = {
        u_tbl["pk"]: chat_id,
        u_tbl["tg_id"]: user.id,
        u_tbl["username"]: user.username,
        u_tbl["first_name"]: user.first_name,
        u_tbl["last_name"]: user.last_name,
        u_tbl["name"]: (user.full_name or "").strip() if hasattr(user, "full_name") else None,
    }

    # SELECT существует ли запись по chat_id
    sel_sql = text(f"""
        SELECT 1 FROM users WHERE {u_tbl['pk']} = :chat_id LIMIT 1
    """)
    # INSERT / UPDATE
    ins_sql = text(f"""
        INSERT INTO users ({u_tbl['pk']}, {u_tbl['tg_id']}, {u_tbl['username']},
                           {u_tbl['first_name']}, {u_tbl['last_name']}, {u_tbl['name']})
        VALUES (:{u_tbl['pk']}, :{u_tbl['tg_id']}, :{u_tbl['username']},
                :{u_tbl['first_name']}, :{u_tbl['last_name']}, :{u_tbl['name']})
    """)
    upd_sql = text(f"""
        UPDATE users
           SET {u_tbl['tg_id']} = :{u_tbl['tg_id']},
               {u_tbl['username']} = :{u_tbl['username']},
               {u_tbl['first_name']} = :{u_tbl['first_name']},
               {u_tbl['last_name']} = :{u_tbl['last_name']},
               {u_tbl['name']} = :{u_tbl['name']},
               {u_tbl['updated_at']} = now()
         WHERE {u_tbl['pk']} = :{u_tbl['pk']}
    """)

    with engine.begin() as conn:
        exists = conn.execute(sel_sql, {"chat_id": chat_id}).first() is not None
        if exists:
            conn.execute(upd_sql, payload)
        else:
            conn.execute(ins_sql, payload)

# ---------- OpenAI (безопасная обёртка) ----------
# Важно: ВСЕ диалоги — только через OpenAI; если ключа нет/ошибка — отвечаем заглушкой.
def generate_reply_via_openai(prompt: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    try:
        # ленивый импорт чтобы не тянуть при отсутствии ключа
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        # очень компактный системный промпт — держим как было ранее
        system = (
            "Ты — весёлая собутыльница Катя. Отвечай коротко, дружелюбно, по-русски. "
            "Не обсуждай политику, не давай медицинских/юридических советов."
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("OpenAI error: %s", e)
        return None

# ---------- Telegram ----------
tapp: Optional[Application] = None

def build_telegram_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start
    async def start_cmd(update: Update, context):
        await upsert_user(update)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Привет! Я Катя 🍷 Что пьём?")

    # текстовые сообщения
    async def text_handler(update: Update, context):
        await upsert_user(update)
        text_in = (update.effective_message.text or "").strip()
        # 1) если увидели напиток — отправим соответствующий стикер
        if text_in:
            low = text_in.lower()
            chosen_key = None
            for kw, sticker_key in DRINK_KEYWORDS.items():
                if kw in low:
                    chosen_key = sticker_key
                    break
            if chosen_key:
                sticker_id = STICKERS[chosen_key]
                try:
                    await context.bot.send_sticker(update.effective_chat.id, sticker_id)
                except Exception as e:
                    logger.warning("Sticker send failed: %s", e)

        # 2) все ответы — через OpenAI (или заглушка, если недоступен)
        reply = generate_reply_via_openai(text_in)
        if reply is None:
            # Не продолжаем диалог, только отдадим фиксированную фразу
            await context.bot.send_message(chat_id=update.effective_chat.id, text=FALLBACK_OPENAI_UNAVAILABLE)
            return
        await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return application

async def ensure_webhook(app: Application) -> None:
    if not AUTO_SET_WEBHOOK:
        return
    if not APP_BASE_URL:
        logger.warning("APP_BASE_URL is empty — webhook не будет установлен автоматически")
        return
    url = f"{APP_BASE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    try:
        await app.bot.set_webhook(url)
        logger.info("✅ Webhook set to %s", url)
    except HTTPError as e:
        logger.error("Failed to set webhook: %s", e)

# ---------- FastAPI ----------
api = FastAPI()

class TelegramUpdate(BaseModel):
    update_id: int | None = None

@api.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

@api.get("/health", response_class=PlainTextResponse)
async def health():
    return "healthy"

@api.on_event("startup")
async def on_startup():
    try:
        init_db()
        global tapp
        tapp = build_telegram_app()
        # важно: инициализируем аппу, чтобы потом можно было process_update()
        await tapp.initialize()
        await ensure_webhook(tapp)
        logger.info("✅ Telegram application is ready")
    except Exception as e:
        logger.error("Startup failed: %s", e)

@api.on_event("shutdown")
async def on_shutdown():
    global tapp
    if tapp:
        try:
            await tapp.shutdown()
            await tapp.stop()
        except Exception:
            pass

@api.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        data = {}

    # Принимаем update и передаём в PTB
    global tapp
    if not tapp:
        raise HTTPException(status_code=500, detail="Bot is not initialized")

    update = Update.de_json(data, tapp.bot)
    await tapp.process_update(update)
    return PlainTextResponse("OK")

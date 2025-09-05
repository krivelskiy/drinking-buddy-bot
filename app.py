import os
import json
import logging
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from constants import (
    STICKERS,
    DRINK_KEYWORDS,
    DB_FIELDS,
    FALLBACK_OPENAI_UNAVAILABLE,
)
from config import DATABASE_URL, OPENAI_API_KEY, WEBHOOK_URL, BOT_TOKEN


# ---------- ЛОГИ ----------

logger = logging.getLogger("app")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# ---------- FASTAPI ----------
app = FastAPI(title="Drinking Buddy Bot")

# ---------- DB ----------
engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

UF = DB_FIELDS["users"]  # сокращение


def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def ensure_user_row(update: Update):
    """Создаём/обновляем пользователя (upsert по chat_id) без «зашивки» полей."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    with engine.begin() as conn:
        # есть ли запись?
        exists = conn.execute(
            text(f"SELECT 1 FROM users WHERE {UF['pk']} = :cid"),
            {"cid": chat_id},
        ).first()

        payload = {
            "chat_id": chat_id,
            "tg_id": user.id if user else None,
            "username": (user.username or None) if user else None,
            "first_name": (user.first_name or None) if user else None,
            "last_name": (user.last_name or None) if user else None,
            "updated_at": _now_str(),
        }

        if exists:
            conn.execute(
                text(
                    f"""
                    UPDATE users
                    SET {UF['tg_id']}=:tg_id,
                        {UF['username']}=:username,
                        {UF['first_name']}=:first_name,
                        {UF['last_name']}=:last_name,
                        {UF['updated_at']}=:updated_at
                    WHERE {UF['pk']}=:chat_id
                    """
                ),
                payload,
            )
        else:
            payload.update(
                {
                    "name": None,
                    "favorite_drinks": json.dumps([]),
                    "summary": None,
                    "free_drinks": 0,
                    "created_at": _now_str(),
                }
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO users
                        ({UF['pk']},{UF['tg_id']},{UF['username']},{UF['first_name']},
                         {UF['last_name']},{UF['name']},{UF['favorite_drinks']},
                         {UF['summary']},{UF['free_drinks']},{UF['created_at']},{UF['updated_at']})
                    VALUES
                        (:chat_id,:tg_id,:username,:first_name,:last_name,
                         :name,:favorite_drinks,:summary,:free_drinks,:created_at,:updated_at)
                    """
                ),
                payload,
            )


def get_user_summary(chat_id: int) -> str:
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {UF['summary']} FROM users WHERE {UF['pk']}=:cid"),
            {"cid": chat_id},
        ).first()
        return row[0] or "" if row else ""


def set_user_summary(chat_id: int, new_summary: str):
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE users SET {UF['summary']}=:summary,{UF['updated_at']}=:ts WHERE {UF['pk']}=:cid"
            ),
            {"summary": new_summary, "ts": _now_str(), "cid": chat_id},
        )


def append_turn_to_summary(chat_id: int, user_text: str, bot_text: str, max_len: int = 8000):
    """Храним краткую «сжатую» историю в users.summary (персистентно)."""
    old = get_user_summary(chat_id)
    line_u = f"[U] {user_text.strip()}"
    line_b = f"[B] {bot_text.strip()}"
    new = (old + "\n" if old else "") + f"{line_u}\n{line_b}"
    # Усечение по длине (с головы)
    if len(new) > max_len:
        new = new[-max_len:]
        # подрезаем до начала строки
        new = new[new.find("\n") + 1 :] if "\n" in new else new
    set_user_summary(chat_id, new)


# ---------- OpenAI ----------
client: OpenAI | None = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
    except Exception as e:
        logger.exception("OpenAI init failed: %s", e)
        client = None
else:
    logger.warning("⚠️ OPENAI_API_KEY пуст — диалог будет отключён")


# ---------- Telegram ----------
tapp: Application | None = None


def build_telegram_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    return (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(set_webhook_on_startup)
        .build()
    )


async def set_webhook_on_startup(app_: Application):
    """Ставим вебхук при инициализации приложения Telegram."""
    if not WEBHOOK_URL:
        logger.warning("⚠️ WEBHOOK_URL пуст — вебхук не будет поставлен")
        return
    url = f"{WEBHOOK_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    ok = await app_.bot.set_webhook(url=url)
    if ok:
        logger.info("✅ Webhook set to %s", url)
    else:
        logger.error("❌ Failed to set webhook to %s", url)


def pick_drink_sticker(text_in: str) -> str | None:
    t = (text_in or "").lower()
    for kw, key in DRINK_KEYWORDS.items():
        if kw in t:
            return STICKERS.get(key)
    return None


async def handle_text(update: Update, context):
    chat_id = update.effective_chat.id
    ensure_user_row(update)

    user_msg = update.effective_message.text or ""

    # Стикер за напиток — отдельно от диалога
    st = pick_drink_sticker(user_msg)
    if st:
        try:
            await context.bot.send_sticker(chat_id=chat_id, sticker=st)
        except Exception as e:
            logger.warning("Sticker send failed: %s", e)

    # Если OpenAI недоступен — строгое поведение-заглушка
    if client is None:
        await context.bot.send_message(chat_id=chat_id, text=FALLBACK_OPENAI_UNAVAILABLE)
        # Историю не продолжаем (по вашим требованиям — «не вести диалог»)
        return

    # Подмешиваем долговременную «сжатую» память
    summary = get_user_summary(chat_id).strip()
    sys_prompt = (
        "Ты — Катя Собутыльница. Отвечай дружелюбно, кратко, по-русски. "
        "Поддерживай лёгкую атмосферу бара. Если пользователь сообщил факты о себе, помни их."
    )

    messages = [
        {"role": "system", "content": sys_prompt},
    ]
    if summary:
        messages.append(
            {
                "role": "system",
                "content": f"Краткая долговременная память о пользователе и прошлом общении:\n{summary}",
            }
        )
    messages.append({"role": "user", "content": user_msg})

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.6,
        )
        bot_text = (resp.choices[0].message.content or "").strip() or "Эм…"
    except Exception as e:
        logger.warning("OpenAI failed: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=FALLBACK_OPENAI_UNAVAILABLE)
        return

    await context.bot.send_message(chat_id=chat_id, text=bot_text)

    # После удачного ответа — сохраняем ход диалога в summary
    try:
        append_turn_to_summary(chat_id, user_msg, bot_text)
    except Exception as e:
        logger.warning("append_turn_to_summary failed: %s", e)


# ---------- FastAPI схемы ----------
class TelegramUpdate(BaseModel):
    update_id: int | None = None
    message: dict | None = None
    edited_message: dict | None = None
    callback_query: dict | None = None
    # остальные поля нам не критичны для валидации


# ---------- ROUTES ----------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    # принимаем только от нашего бота
    if token != BOT_TOKEN:
        return Response(status_code=403)

    data = await request.json()
    update = Update.de_json(data, bot=tapp.bot)  # type: ignore
    await tapp.process_update(update)  # type: ignore
    return Response(status_code=200)


# ---------- LIFECYCLE ----------
@app.on_event("startup")
async def on_startup():
    global tapp
    tapp = build_telegram_app()
    # основной обработчик текста
    tapp.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    await tapp.initialize()
    await tapp.start()
    logger.info("✅ Telegram application started")


@app.on_event("shutdown")
async def on_shutdown():
    if tapp:
        await tapp.stop()
        await tapp.shutdown()
        logger.info("🛑 Telegram application stopped")

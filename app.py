import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# ВАЖНО: всё, что связано со схемой и именами полей, берём только из constants
from constants import STICKERS, DRINK_KEYWORDS, DB_FIELDS, FALLBACK_OPENAI_UNAVAILABLE

# ------- конфиг ключей (строго по договорённостям)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE_URL = os.getenv("BASE_URL", os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")

# ------- логирование
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# ------- FastAPI app (обязательно app)
app = FastAPI(title="Drinking Buddy Bot", version="1.0.1")

# =========================================
# DB
# =========================================
_engine: Optional[Engine] = None


def _init_db_engine() -> Engine:
    if not DATABASE_URL:
        logger.warning("DATABASE_URL is empty — DB features will be disabled")
    eng = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
    return eng


def db_exec(sql: str, **params):
    global _engine
    if _engine is None:
        _engine = _init_db_engine()
        logger.info("✅ Database initialized")
    with _engine.begin() as conn:
        return conn.execute(text(sql), params)


def upsert_user_from_update(message: Dict[str, Any]) -> None:
    """Создаёт/обновляет пользователя по incoming message (имена полей из DB_FIELDS)."""
    if not DATABASE_URL:
        return

    users = DB_FIELDS["users"]  # Имена колонок берём только из constants
    chat = message.get("chat") or {}
    user = message.get("from") or {}

    chat_id = chat.get("id")
    tg_id = user.get("id")
    username = user.get("username")
    first_name = user.get("first_name")
    last_name = user.get("last_name")

    if chat_id is None or tg_id is None:
        return

    sql = f"""
    INSERT INTO users ({users['pk']}, {users['tg_id']}, {users['username']}, {users['first_name']}, {users['last_name']})
    VALUES (:chat_id, :tg_id, :username, :first_name, :last_name)
    ON CONFLICT ({users['pk']}) DO UPDATE SET
        {users['tg_id']} = EXCLUDED.{users['tg_id']},
        {users['username']} = EXCLUDED.{users['username']},
        {users['first_name']} = EXCLUDED.{users['first_name']},
        {users['last_name']} = EXCLUDED.{users['last_name']},
        {users['updated_at']} = now();
    """
    db_exec(
        sql,
        chat_id=chat_id,
        tg_id=tg_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
    )


def load_user_context(chat_id: int) -> Dict[str, Any]:
    """Возвращает summary/favorite_drinks/name как контекст (имена полей из DB_FIELDS)."""
    if not DATABASE_URL:
        return {}
    users = DB_FIELDS["users"]
    sql = f"""
        SELECT {users['summary']} AS summary,
               {users['favorite_drinks']} AS favorite_drinks,
               {users['name']} AS name
        FROM users
        WHERE {users['pk']} = :chat_id
    """
    row = db_exec(sql, chat_id=chat_id).mappings().first()
    if not row:
        return {}
    return {
        "summary": row.get("summary"),
        "favorite_drinks": row.get("favorite_drinks") or [],
        "name": row.get("name"),
    }


# =========================================
# Telegram API (без PTB)
# =========================================
TG_API = "https://api.telegram.org"


async def tg_send_text(chat_id: int, text: str) -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is empty — cannot send messages")
        return
    url = f"{TG_API}/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload)
        if r.status_code != 200:
            logger.error("sendMessage failed: %s %s", r.status_code, r.text)


async def tg_send_sticker(chat_id: int, sticker_id: str) -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is empty — cannot send stickers")
        return
    url = f"{TG_API}/bot{BOT_TOKEN}/sendSticker"
    payload = {"chat_id": chat_id, "sticker": sticker_id}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload)
        if r.status_code != 200:
            logger.error("sendSticker failed: %s %s", r.status_code, r.text)


async def tg_set_webhook() -> None:
    if not (BOT_TOKEN and BASE_URL):
        if not BOT_TOKEN:
            logger.warning("BOT_TOKEN is empty (webhook работать не будет)")
        if not BASE_URL:
            logger.warning("BASE_URL is empty (webhook не будет выставлен)")
        return
    url = f"{TG_API}/bot{BOT_TOKEN}/setWebhook"
    webhook_url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json={"url": webhook_url})
        if r.status_code == 200:
            logger.info("✅ Webhook set to %s", webhook_url)
        else:
            logger.error("setWebhook failed: %s %s", r.status_code, r.text)


# =========================================
# OpenAI
# =========================================
def build_openai_client() -> Optional[OpenAI]:
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY is empty — conversations will use fallback")
        return None
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
        return client
    except Exception as e:
        logger.exception("OpenAI init failed: %s", e)
        return None


_openai_client: Optional[OpenAI] = None


async def ai_reply(user_text: str, user_ctx: Dict[str, Any]) -> Optional[str]:
    """Все диалоги только через OpenAI. При сбое — вернём None (отправим заглушку)."""
    global _openai_client
    if _openai_client is None:
        _openai_client = build_openai_client()
    if _openai_client is None:
        return None

    summary = (user_ctx.get("summary") or "").strip()
    fav = user_ctx.get("favorite_drinks") or []
    name = (user_ctx.get("name") or "").strip()

    sys_parts = [
        "Ты — Катя Собутыльница: дружелюбная, остроумная, позитивная.",
        "Отвечай коротко и по делу, с лёгкой иронией. Не повторяй сообщения пользователя.",
        "Если собеседник спросит факты о себе — используй контекст из БД, если он есть.",
        "Никаких платёжных сценариев сейчас не запускай.",
    ]
    if name:
        sys_parts.append(f"Имя собеседника: {name}.")
    if summary:
        sys_parts.append(f"Краткое описание собеседника: {summary}.")
    if fav:
        sys_parts.append(f"Любимые напитки собеседника: {', '.join(map(str, fav))}.")

    system_prompt = " ".join(sys_parts)

    try:
        resp = _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature=0.7,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return None


# =========================================
# Startup
# =========================================
@app.on_event("startup")
async def on_startup():
    if DATABASE_URL:
        global _engine
        _engine = _init_db_engine()
    else:
        logger.warning("DATABASE_URL is empty (DB features disabled)")
    await tg_set_webhook()


# =========================================
# Routes
# =========================================
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "healthy"


@app.get("/db/schema", response_class=JSONResponse)
async def db_schema():
    """
    Возвращаем структуру ТОЛЬКО из constants.DB_FIELDS,
    без хардкода типов/названий в приложении.
    """
    return {"db_fields": DB_FIELDS}


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """
    Принимаем апдейты Telegram.
    Требуем строгого совпадения token в пути с BOT_TOKEN — иначе 403.
    """
    if not BOT_TOKEN or token != BOT_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="wrong token in path")

    payload = await request.json()
    logger.info("Incoming update: %s", payload.get("update_id"))

    message = payload.get("message") or payload.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return {"ok": True}

    text = (message.get("text") or "").strip()

    # 1) апсертим пользователя (имена колонок из constants)
    try:
        upsert_user_from_update(message)
    except Exception as e:
        logger.exception("User upsert failed: %s", e)

    # 2) распознаём напиток — отправляем стикер из constants.STICKERS
    lowered = text.lower()
    for kw, sticker_key in DRINK_KEYWORDS.items():
        if kw in lowered:
            sticker_id = STICKERS[sticker_key]
            await tg_send_sticker(chat_id, sticker_id)
            break

    # 3) отвечаем ТОЛЬКО через OpenAI; при ошибке — заглушка и без продолжения
    user_ctx = {}
    try:
        user_ctx = load_user_context(chat_id)
    except Exception as e:
        logger.exception("load_user_context failed: %s", e)

    reply = await ai_reply(text, user_ctx)
    if reply is None:
        await tg_send_text(chat_id, FALLBACK_OPENAI_UNAVAILABLE)
        return {"ok": True}

    await tg_send_text(chat_id, reply)
    return {"ok": True}

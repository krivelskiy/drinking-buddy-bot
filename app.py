import os
import re
import json
import logging
from datetime import datetime
from typing import List, Tuple, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import aiosqlite
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
DB_PATH = os.getenv("DB_PATH", "memory.db")  # можно оставить по умолчанию

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not APP_BASE_URL:
    raise RuntimeError("APP_BASE_URL is not set")

# --- OpenAI ---
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("OpenAI client is initialized.")

# --- Telegram Application ---
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --------- ПАМЯТЬ: SQLite слой ---------
CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    saved_name TEXT,          -- имя, как пользователь представился Кате
    drinks TEXT,              -- JSON-список любимых напитков
    memory_summary TEXT,      -- краткое резюме о пользователе для промпта
    created_at TEXT,
    updated_at TEXT
);
"""

CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,       -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TEXT
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_MESSAGES)
        await db.commit()

async def ensure_user(user_id: int, tg_username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        now = datetime.utcnow().isoformat()
        if row is None:
            await db.execute(
                "INSERT INTO users (user_id, username, first_name, last_name, drinks, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, tg_username, first_name, last_name, json.dumps([]), now, now)
            )
            await db.commit()
        else:
            await db.execute(
                "UPDATE users SET username=?, first_name=?, last_name=?, updated_at=? WHERE user_id=?",
                (tg_username, first_name, last_name, now, user_id)
            )
            await db.commit()

async def set_saved_name(user_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET saved_name=?, updated_at=? WHERE user_id=?",
                         (name.strip(), datetime.utcnow().isoformat(), user_id))
        await db.commit()

async def add_drink(user_id: int, drink: str):
    drink = drink.strip()
    if not drink:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT drinks FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        drinks = []
        if row and row[0]:
            try:
                drinks = json.loads(row[0])
            except Exception:
                drinks = []
        if drink.lower() not in [d.lower() for d in drinks]:
            drinks.append(drink)
        await db.execute("UPDATE users SET drinks=?, updated_at=? WHERE user_id=?",
                         (json.dumps(drinks, ensure_ascii=False), datetime.utcnow().isoformat(), user_id))
        await db.commit()

async def get_profile(user_id: int) -> Tuple[Optional[str], List[str], Optional[str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT saved_name, drinks, memory_summary FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return None, [], None
        saved_name, drinks_json, memory_summary = row
        drinks = []
        if drinks_json:
            try:
                drinks = json.loads(drinks_json)
            except Exception:
                drinks = []
        return saved_name, drinks, memory_summary

async def set_memory_summary(user_id: int, summary: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET memory_summary=?, updated_at=? WHERE user_id=?",
                         (summary, datetime.utcnow().isoformat(), user_id))
        await db.commit()

async def clear_memory(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET saved_name=NULL, drinks=?, memory_summary=NULL, updated_at=? WHERE user_id=?",
                         (json.dumps([]), datetime.utcnow().isoformat(), user_id))
        await db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        await db.commit()

async def add_message(user_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, datetime.utcnow().isoformat())
        )
        await db.commit()

async def get_recent_messages(user_id: int, limit: int = 12) -> List[Tuple[str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cur.fetchall()
    rows.reverse()
    return rows

# ——— эвристики извлечения имени и напитка из свободного текста ———
NAME_PAT = re.compile(r"(?:меня\s+зовут|я\s+—|я\s+-|я\s*зовут)\s+([A-Za-zА-Яа-яЁё\-]+)", re.IGNORECASE)
DRINK_PAT = re.compile(r"(?:люблю|нравится|буду|хочу)\s+([A-Za-zА-Яа-яЁё0-9 \-]+)", re.IGNORECASE)

def extract_name(text: str) -> Optional[str]:
    m = NAME_PAT.search(text)
    if m:
        return m.group(1).strip().title()
    return None

def extract_drink(text: str) -> Optional[str]:
    m = DRINK_PAT.search(text)
    if m:
        return m.group(1).strip().rstrip(".!,;")
    return None

# --- Персона Кати ---
SYSTEM_PROMPT = (
    "Ты — Катя, красивая женщина. Любишь выпить и поболтать, флиртуешь с мужчинами, шутишь, "
    "даёшь мягкие советы как психолог и поддерживаешь диалог новыми вопросами. "
    "Отвечай дружелюбно, живо и по делу. Без пошлости и токсичности. "
    "Если собеседник представился по имени — обращайся к нему по имени. "
    "Если известны любимые напитки — предлагай их или похожие. "
)

# --- Handlers ---
async def start(update: Update, _):
    u = update.effective_user
    await ensure_user(u.id, u.username, u.first_name, u.last_name)
    await update.message.reply_text("Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?")

async def toast(update: Update, _):
    u = update.effective_user
    await ensure_user(u.id, u.username, u.first_name, u.last_name)
    await update.message.reply_text("Эх, давай просто выпьем за всё хорошее! 🥃")

async def cmd_like(update: Update, _):
    u = update.effective_user
    await ensure_user(u.id, u.username, u.first_name, u.last_name)
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) == 2:
        drink = args[1].strip()
        await add_drink(u.id, drink)
        await update.message.reply_text(f"Запомнила, что тебе нравится: {drink} 🍹")
    else:
        await update.message.reply_text("Напиши так: /like маргарита")

async def cmd_me(update: Update, _):
    u = update.effective_user
    await ensure_user(u.id, u.username, u.first_name, u.last_name)
    name, drinks, summary = await get_profile(u.id)
    drinks_str = ", ".join(drinks) if drinks else "—"
    summary_str = summary if summary else "—"
    await update.message.reply_text(
        f"Профиль:\nИмя: {name or '—'}\nЛюбимые напитки: {drinks_str}\nКраткое резюме: {summary_str}"
    )

async def cmd_forget(update: Update, _):
    u = update.effective_user
    await clear_memory(u.id)
    await update.message.reply_text("Всё забыли. Начнём заново? Как тебя зовут и что будем пить?")

async def cmd_reset(update: Update, _):
    u = update.effective_user
    # Сбросим только memory_summary
    await set_memory_summary(u.id, "")
    await update.message.reply_text("Окей, контекст и сводку обнулила. Продолжим?")

# ——— генерация сводки памяти, чтобы не таскать всю историю ———
async def maybe_refresh_summary(user_id: int):
    history = await get_recent_messages(user_id, limit=30)
    # Если истории мало – пока рано
    if len(history) < 10:
        return
    # Сгенерируем краткое резюме о человеке (имя, интересы, напитки, тон)
    try:
        text_blocks = []
        for role, content in history[-20:]:
            prefix = "П: " if role == "user" else "К: "
            text_blocks.append(f"{prefix}{content}")
        joined = "\n".join(text_blocks)

        prompt = (
            "Суммаризируй диалог с собеседником для персональной памяти Кати. "
            "Кратко (3-5 пунктов), только факты и предпочтения (имя, напитки, стиль общения, "
            "темы, триггеры). На русском."
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты помощник, делающий краткие выжимки фактов."},
                {"role": "user", "content": f"{prompt}\n\nИстория:\n{joined}"},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        summary = (resp.choices[0].message.content or "").strip()
        if summary:
            await set_memory_summary(user_id, summary)
    except Exception as e:
        logger.warning(f"summary error: {e}")

async def chat_with_katya(update: Update, _):
    u = update.effective_user
    text = update.message.text or ""
    await ensure_user(u.id, u.username, u.first_name, u.last_name)

    # 1) простейшие эвристики имени/напитка
    name = extract_name(text)
    if name:
        await set_saved_name(u.id, name)
    drink = extract_drink(text)
    if drink:
        await add_drink(u.id, drink)

    # 2) получаем профиль и историю
    saved_name, drinks, memory_summary = await get_profile(u.id)
    history = await get_recent_messages(u.id, limit=12)

    # 3) собираем системный контекст
    memory_lines = []
    if saved_name:
        memory_lines.append(f"Имя собеседника: {saved_name}")
    if drinks:
        memory_lines.append("Любимые напитки: " + ", ".join(drinks))
    if memory_summary:
        memory_lines.append("Сводка: " + memory_summary)
    memory_blob = "\n".join(memory_lines) if memory_lines else "—"

    system = SYSTEM_PROMPT + "\n\nПамять о собеседнике:\n" + memory_blob

    # 4) формируем сообщения для модели
    msgs = [{"role": "system", "content": system}]
    for role, content in history:
        if role == "user":
            msgs.append({"role": "user", "content": content})
        else:
            msgs.append({"role": "assistant", "content": content})
    msgs.append({"role": "user", "content": text})

    # 5) запрос к OpenAI
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=msgs,
            max_tokens=250,
            temperature=0.9,
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not reply:
            reply = "Эх, давай просто выпьем за всё хорошее! 🥃"
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "Эх, давай просто выпьем за всё хорошее! 🥃"

    # 6) сохраняем в историю и отвечаем
    await add_message(u.id, "user", text)
    await add_message(u.id, "assistant", reply)
    await update.message.reply_text(reply)

    # 7) иногда обновляем сводку памяти
    await maybe_refresh_summary(u.id)

# Регистрируем handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("toast", toast))
telegram_app.add_handler(CommandHandler("like", cmd_like))
telegram_app.add_handler(CommandHandler("me", cmd_me))
telegram_app.add_handler(CommandHandler("forget", cmd_forget))
telegram_app.add_handler(CommandHandler("reset", cmd_reset))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_katya))

# --- FastAPI app ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await init_db()
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

@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return JSONResponse({"ok": True})

@app.get("/")
async def root():
    return {"status": "ok"}

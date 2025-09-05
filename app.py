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
# Утилиты БД
# -----------------------------
U = DB_FIELDS["users"]  # короткий алиас
M = DB_FIELDS["messages"]  # короткий алиас

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

def init_db():
    """Создание таблиц если их нет"""
    with engine.begin() as conn:
        # Создание таблицы users
        conn.execute(DDL(f"""
            CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
                id SERIAL PRIMARY KEY,
                user_tg_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                age INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        
        # Создание таблицы messages
        conn.execute(DDL(f"""
            CREATE TABLE IF NOT EXISTS {MESSAGES_TABLE} (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_tg_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                reply_to_message_id INTEGER,
                message_id INTEGER
            )
        """))
        
        # Добавляем недостающие колонки к существующим таблицам
        try:
            # Добавляем колонку user_tg_id к users если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {USERS_TABLE} 
                ADD COLUMN IF NOT EXISTS user_tg_id BIGINT UNIQUE
            """))
        except Exception:
            pass  # Колонка уже существует
            
        try:
            # Добавляем колонку age к users если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {USERS_TABLE} 
                ADD COLUMN IF NOT EXISTS age INTEGER
            """))
        except Exception:
            pass  # Колонка уже существует
            
        try:
            # Добавляем колонку user_tg_id к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS user_tg_id BIGINT NOT NULL DEFAULT 0
            """))
        except Exception:
            pass  # Колонка уже существует
            
        try:
            # Добавляем колонку message_id к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS message_id INTEGER
            """))
        except Exception:
            pass  # Колонка уже существует
            
        try:
            # Добавляем колонку reply_to_message_id к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS reply_to_message_id INTEGER
            """))
        except Exception:
            pass  # Колонка уже существует
    
    logger.info("✅ Database tables created/verified")

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
M = DB_FIELDS["messages"]  # короткий алиас

def upsert_user_from_update(update: Update) -> None:
    """
    Без ON CONFLICT, чтобы не зависеть от уникальных ограничений:
    1) ищем пользователя по user_tg_id
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
            text(f"SELECT {U['user_tg_id']} FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": tg_id},
        ).fetchone()

        if row:
            conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE}
                    SET {U['username']} = :username,
                        {U['first_name']} = :first_name,
                        {U['last_name']} = :last_name
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {
                    "tg_id": tg_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )
        else:
            conn.execute(
                text(f"""
                    INSERT INTO {USERS_TABLE} ({U['user_tg_id']}, {U['username']}, {U['first_name']}, {U['last_name']})
                    VALUES (:tg_id, :username, :first_name, :last_name)
                """),
                {
                    "tg_id": tg_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )

def save_message(chat_id: int, user_tg_id: int, role: str, content: str, message_id: Optional[int] = None, reply_to_message_id: Optional[int] = None) -> None:
    """Сохранение сообщения в БД"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                INSERT INTO {MESSAGES_TABLE} ({M['chat_id']}, {M['user_tg_id']}, {M['role']}, {M['content']}, {M['message_id']}, {M['reply_to_message_id']})
                VALUES (:chat_id, :user_tg_id, :role, :content, :message_id, :reply_to_message_id)
            """),
            {
                "chat_id": chat_id,
                "user_tg_id": user_tg_id,
                "role": role,
                "content": content,
                "message_id": message_id,
                "reply_to_message_id": reply_to_message_id,
            },
        )

def get_user_age(user_tg_id: int) -> Optional[int]:
    """Получение возраста пользователя из БД"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {U['age']} FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        return row[0] if row and row[0] else None

def update_user_age(user_tg_id: int, age: int) -> None:
    """Обновление возраста пользователя в БД"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET {U['age']} = :age
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id, "age": age},
        )

def get_recent_messages(chat_id: int, limit: int = 10) -> list:
    """Получение последних сообщений для контекста"""
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"""
                SELECT {M['role']}, {M['content']} 
                FROM {MESSAGES_TABLE} 
                WHERE {M['chat_id']} = :chat_id 
                ORDER BY {M['created_at']} DESC 
                LIMIT :limit
            """),
            {"chat_id": chat_id, "limit": limit},
        ).fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

def parse_age_from_text(text: str) -> Optional[int]:
    """Простой парсер возраста из текста"""
    # Ищем паттерны типа "мне 25 лет", "25 лет", "я 25-летний" и т.п.
    patterns = [
        r'мне\s+(\d+)\s+лет',
        r'(\d+)\s+лет',
        r'я\s+(\d+)[-]?летний',
        r'(\d+)[-]?летний',
        r'возраст\s+(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            try:
                age = int(match.group(1))
                if 1 <= age <= 150:  # разумные границы возраста
                    return age
            except ValueError:
                continue
    
    return None

async def send_beer_sticker(chat_id: int) -> None:
    """Отправка стикера пива через Telegram API"""
    if not BOT_TOKEN or not BEER_STICKERS:
        return
    
    sticker_id = BEER_STICKERS[0]  # берем первый стикер из списка
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker",
                json={
                    "chat_id": chat_id,
                    "sticker": sticker_id
                }
            )
            if response.status_code == 200:
                logger.info("✅ Beer sticker sent successfully")
            else:
                logger.warning("Failed to send sticker: %s", response.text)
        except Exception as e:
            logger.exception("Error sending sticker: %s", e)

# -----------------------------
# Генерация ответа
# -----------------------------
def load_context() -> str:
    """Загрузка контекста из Context.txt"""
    try:
        with open("Context.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()
            # Убираем первую строку с инструкциями для разработчика
            lines = content.split('\n')
            if lines[0].startswith('Это файл с правилами разработки'):
                content = '\n'.join(lines[1:]).strip()
            return content
    except Exception:
        return "Ты — Катя Собутыльница: дружелюбная, немного флиртуешь, любишь пошутить. Отвечай коротко и по делу. Если пользователь сообщает факт о себе (возраст, имя, любимые напитки), можешь подтвердить и не забывай про атмосферу бара."

SYSTEM_PROMPT = load_context()

async def llm_reply(user_text: str, username: Optional[str], user_tg_id: int, chat_id: int) -> str:
    if client is None:
        return FALLBACK_OPENAI_UNAVAILABLE
    
    try:
        # Получаем возраст пользователя
        user_age = get_user_age(user_tg_id)
        
        # Получаем историю сообщений
        recent_messages = get_recent_messages(chat_id, limit=5)
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        
        # Добавляем информацию о пользователе
        if user_age:
            messages.append({"role": "system", "content": f"Пользователю {user_age} лет."})
        if username:
            messages.append({"role": "system", "content": f"Username пользователя: @{username}"})
        
        # Добавляем историю сообщений (в обратном порядке для правильной последовательности)
        for msg in reversed(recent_messages[-3:]):  # только последние 3 сообщения
            messages.append(msg)
        
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

    chat_id = update.effective_chat.id  # type: ignore
    user_tg_id = update.effective_user.id if update.effective_user else None  # type: ignore
    username = update.effective_user.username if update.effective_user else None  # type: ignore
    first_name = update.effective_user.first_name if update.effective_user else None  # type: ignore
    text_in = update.message.text
    message_id = update.message.message_id

    if user_tg_id is None:
        logger.warning("No user_tg_id in update")
        return

    # 1) апсерт пользователя
    try:
        upsert_user_from_update(update)
    except Exception:
        logger.exception("Failed to upsert user — продолжим без записи")

    # 2) Сохраняем входящее сообщение
    try:
        save_message(chat_id, user_tg_id, "user", text_in, message_id)
    except Exception:
        logger.exception("Failed to save user message")

    # 3) Проверяем на упоминание возраста
    age = parse_age_from_text(text_in)
    if age:
        try:
            update_user_age(user_tg_id, age)
            logger.info("Updated user age to %d", age)
        except Exception:
            logger.exception("Failed to update user age")

    # 4) Проверяем на триггеры стикеров
    lower_text = text_in.lower()
    should_send_sticker = any(trigger in lower_text for trigger in STICKER_TRIGGERS)
    
    if should_send_sticker:
        try:
            await send_beer_sticker(chat_id)
        except Exception:
            logger.exception("Failed to send beer sticker")

    # 5) Генерируем ответ через OpenAI
    answer = await llm_reply(text_in, username, user_tg_id, chat_id)

    # 6) Отправляем ответ
    try:
        sent_message = await update.message.reply_text(answer)
        # Сохраняем ответ бота
        save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id)
    except Exception:
        logger.exception("Failed to send reply")

    # 7) Если упомянут напиток — отправляем соответствующий стикер
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
    
    # Инициализируем БД
    try:
        init_db()
    except Exception:
        logger.exception("Failed to initialize database")
    
    tapp = build_application()
    await tapp.initialize()
    await tapp.start()

    # Ставим вебхук, если можем вычислить внешний URL
    if RENDER_EXTERNAL_URL:
        try:
            await tapp.bot.set_webhook(f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}")
            logger.info("✅ Webhook set to %s/webhook/%s", RENDER_EXTERNAL_URL, BOT_TOKEN)
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

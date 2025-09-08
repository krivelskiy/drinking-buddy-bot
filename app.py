import os
import httpx
import re
import logging
import traceback
from functools import wraps
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional
import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from sqlalchemy import create_engine, text, DDL
from sqlalchemy.engine import Engine

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters, CommandHandler, CallbackQueryHandler, PreCheckoutQueryHandler

from openai import OpenAI

from config import DATABASE_URL, OPENAI_API_KEY, BOT_TOKEN, RENDER_EXTERNAL_URL
from constants import (
    STICKERS, DRINK_KEYWORDS, DB_FIELDS, FALLBACK_OPENAI_UNAVAILABLE,
    USERS_TABLE, MESSAGES_TABLE, BEER_STICKERS, STICKER_TRIGGERS
)

# -----------------------------
# Система безопасности и мониторинга
# -----------------------------

# Счетчики ошибок для мониторинга
error_counts = defaultdict(int)
last_error_time = defaultdict(float)

# -----------------------------
# Системный промпт
# -----------------------------
def load_system_prompt() -> str:
    """Загрузка системного промпта из Context.txt"""
    try:
        with open("Context.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.error(f"Failed to load system prompt: {e}")
        return "Ты — Катя Собутыльница: живая девушка, дружелюбная, немного флиртуешь, любишь пошутить и выпить."

SYSTEM_PROMPT = load_system_prompt()

def safe_execute(func):
    """Декоратор для безопасного выполнения функций"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            error_counts[func.__name__] += 1
            last_error_time[func.__name__] = time.time()
            logger.exception(f"Critical error in {func.__name__}: {e}")
            
            # Отправляем уведомление о критической ошибке
            await notify_critical_error(func.__name__, str(e))
            
            # Возвращаем безопасный fallback
            return get_fallback_response(func.__name__)
    return wrapper

def safe_execute_sync(func):
    """Декоратор для безопасного выполнения синхронных функций"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_counts[func.__name__] += 1
            last_error_time[func.__name__] = time.time()
            logger.exception(f"Critical error in {func.__name__}: {e}")
            return get_fallback_response(func.__name__)
    return wrapper

async def notify_critical_error(function_name: str, error: str) -> None:
    """Уведомление о критической ошибке (можно отправить в мониторинг)"""
    try:
        logger.critical(f"CRITICAL ERROR in {function_name}: {error}")
        # Здесь можно добавить отправку в Slack, Telegram, Sentry и т.д.
    except Exception:
        pass  # Не падаем из-за ошибки в уведомлении

def get_fallback_response(function_name: str) -> str:
    """Безопасный fallback ответ"""
    fallbacks = {
        "llm_reply": "Извини, у меня сейчас технические проблемы. Попробуй позже! 😅",
        "msg_handler": None,  # Не отправляем ответ при ошибке в обработчике
        "send_auto_messages": None,  # Пропускаем автоматические сообщения
        "ping_scheduler": None,  # Пропускаем ping
    }
    return fallbacks.get(function_name, "Что-то пошло не так. Попробуй еще раз! 🤔")

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
# Глобальные переменные
# -----------------------------
tapp: Optional[Application] = None

# -----------------------------
# Инициализация
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
        
        # Добавляем недостающие колонки если их нет
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS chat_id BIGINT"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS tg_id BIGINT UNIQUE"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS age INTEGER"))
        conn.execute(DDL(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN IF NOT EXISTS message_id INTEGER"))
        conn.execute(DDL(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN IF NOT EXISTS reply_to_message_id INTEGER"))
        conn.execute(DDL(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN IF NOT EXISTS sticker_sent BOOLEAN DEFAULT FALSE"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS preferences TEXT"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_preference_ask DATE"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_holiday_suggest TIMESTAMPTZ"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_auto_message TIMESTAMPTZ"))
    
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
# Функции для бесплатных напитков Кати
# -----------------------------

def get_katya_drinks_count(chat_id: int) -> int:
    """Получить количество использованных бесплатных напитков Кати за сегодня"""
    with engine.begin() as conn:
        result = conn.execute(text(f"""
            SELECT drinks_used FROM katya_free_drinks 
            WHERE chat_id = :chat_id AND date_reset = CURRENT_DATE
        """), {"chat_id": chat_id}).fetchone()
        
        if result:
            return result[0]
        return 0

def increment_katya_drinks(chat_id: int) -> None:
    """Увеличить счетчик бесплатных напитков Кати"""
    with engine.begin() as conn:
        # Проверяем, есть ли запись на сегодня
        result = conn.execute(text(f"""
            SELECT id FROM katya_free_drinks 
            WHERE chat_id = :chat_id AND date_reset = CURRENT_DATE
        """), {"chat_id": chat_id}).fetchone()
        
        if result:
            # Обновляем существующую запись
            conn.execute(text(f"""
                UPDATE katya_free_drinks 
                SET drinks_used = drinks_used + 1 
                WHERE chat_id = :chat_id AND date_reset = CURRENT_DATE
            """), {"chat_id": chat_id})
        else:
            # Создаем новую запись
            conn.execute(text(f"""
                INSERT INTO katya_free_drinks (chat_id, drinks_used, date_reset) 
                VALUES (:chat_id, 1, CURRENT_DATE)
            """), {"chat_id": chat_id})

def can_katya_drink_free(chat_id: int) -> bool:
    """Проверить, может ли Катя выпить бесплатно (лимит 5 напитков в день)"""
    return get_katya_drinks_count(chat_id) < 5

# -----------------------------
# Система праздников
# -----------------------------
def get_today_holidays() -> list[str]:
    """Получить праздники на сегодня"""
    today = datetime.now()
    month_day = (today.month, today.day)
    
    # Реальные российские праздники с точными датами
    holidays = {
        (1, 1): "Новый год",
        (1, 7): "Рождество Христово", 
        (1, 14): "Старый Новый год",
        (1, 25): "День студента (Татьянин день)",
        (2, 14): "День святого Валентина",
        (2, 23): "День защитника Отечества",
        (3, 8): "Международный женский день",
        (3, 20): "День весеннего равноденствия",
        (4, 1): "День смеха",
        (4, 12): "День космонавтики",
        (4, 22): "День Земли",
        (5, 1): "День труда",
        (5, 9): "День Победы",
        (5, 15): "День семьи",
        (5, 24): "День славянской письменности",
        (6, 1): "День защиты детей",
        (6, 12): "День России",
        (6, 22): "День памяти и скорби",
        (7, 8): "День семьи, любви и верности",
        (7, 28): "День Крещения Руси",
        (8, 2): "День ВДВ",
        (8, 9): "День строителя",
        (8, 12): "День Военно-воздушных сил",
        (8, 22): "День Государственного флага",
        (8, 27): "День кино",
        (9, 1): "День знаний",
        (9, 5): "День учителя",
        (9, 21): "День мира",
        (9, 27): "День воспитателя",
        (10, 1): "День пожилых людей",
        (10, 5): "День учителя (всемирный)",
        (10, 14): "День стандартизации",
        (10, 25): "День таможенника",
        (10, 30): "День памяти жертв политических репрессий",
        (11, 4): "День народного единства",
        (11, 7): "День Октябрьской революции",
        (11, 10): "День милиции",
        (11, 17): "День участкового",
        (11, 21): "День бухгалтера",
        (11, 27): "День матери",
        (12, 3): "День юриста",
        (12, 10): "День прав человека",
        (12, 12): "День Конституции",
        (12, 20): "День работника органов безопасности",
        (12, 22): "День энергетика",
        (12, 25): "Рождество",
        (12, 31): "Новогодняя ночь"
    }
    
    today_holidays = []
    if month_day in holidays:
        today_holidays.append(holidays[month_day])
    
    return today_holidays

def should_suggest_holiday(user_tg_id: int) -> bool:
    """Проверить, можно ли предложить праздник (не чаще раза в сутки)"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT last_holiday_suggest FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        
        if not row or not row[0]:
            return True
        
        last_suggest = row[0]
        today = datetime.now().date()
        
        return last_suggest.date() < today

def update_last_holiday_suggest(user_tg_id: int) -> None:
    """Обновить дату последнего предложения праздника"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET last_holiday_suggest = NOW()
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id}
        )

# -----------------------------
# Система автоматических сообщений
# -----------------------------
def get_users_for_auto_message() -> list[dict]:
    """Получить пользователей, которым нужно отправить автоматическое сообщение"""
    with engine.begin() as conn:
        # Ищем пользователей, с которыми не общались более 24 часов
        query = f"""
            SELECT DISTINCT u.user_tg_id, u.chat_id, u.first_name, u.preferences
            FROM {USERS_TABLE} u
            LEFT JOIN (
                SELECT user_tg_id, MAX(created_at) as last_message_time
                FROM {MESSAGES_TABLE}
                GROUP BY user_tg_id
            ) m ON u.user_tg_id = m.user_tg_id
            WHERE m.last_message_time IS NULL 
               OR m.last_message_time < NOW() - INTERVAL '24 hours'
               OR u.last_auto_message IS NULL 
               OR u.last_auto_message < NOW() - INTERVAL '24 hours'
        """
        
        rows = conn.execute(text(query)).fetchall()
        return [
            {
                "user_tg_id": row[0],
                "chat_id": row[1], 
                "first_name": row[2],
                "preferences": row[3]
            }
            for row in rows
        ]

def generate_auto_message(first_name: str, preferences: Optional[str]) -> str:
    """Генерировать заманчивое сообщение для пользователя"""
    messages = [
        f"Привет, {first_name}! Соскучилась по нашим разговорам 😊 Давай выпьем и поболтаем?",
        f"Эй, {first_name}! У меня есть отличная идея - давай отметим что-нибудь! 🍻",
        f"{first_name}, я тут думаю... а не выпить ли нам? 😉",
        f"Привет! Скучаю по нашей компании, {first_name}! Давай встретимся за рюмочкой?",
        f"Эй, {first_name}! У меня настроение праздновать! Присоединяешься? 🥂",
        f"Привет! Давай устроим вечеринку на двоих, {first_name}! 🎉",
        f"{first_name}, я тут одна сижу... не соскучишься ли по мне? 😘",
        f"Эй! У меня есть повод выпить! Хочешь узнать какой, {first_name}? 🍷",
        f"Привет, {first_name}! Давай отметим что-нибудь хорошее! 🥃",
        f"{first_name}, я тут думаю о тебе... а не выпить ли нам вместе? 😊"
    ]
    
    # Если есть предпочтения, добавляем их в сообщение
    if preferences:
        pref_messages = [
            f"Привет, {first_name}! У меня есть твое любимое {preferences}! Давай выпьем? 🍻",
            f"Эй, {first_name}! Я приготовила {preferences} специально для тебя! 😘",
            f"{first_name}, помнишь как ты любишь {preferences}? Давай отметим! 🥂",
            f"Привет! У меня есть {preferences} - твой любимый напиток! Присоединяешься? 🥂"
        ]
        messages.extend(pref_messages)
    
    import random
    return random.choice(messages)

def update_last_auto_message(user_tg_id: int) -> None:
    """Обновить дату последнего автоматического сообщения"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET last_auto_message = NOW()
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id}
        )

async def send_auto_messages():
    """Отправить автоматические сообщения пользователям"""
    try:
        users = get_users_for_auto_message()
        logger.info(f"Found {len(users)} users for auto messages")
        
        for user in users:
            try:
                message = generate_auto_message(user["first_name"] or "друг", user["preferences"])
                
                # Отправляем сообщение через Telegram API
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": user["chat_id"],
                            "text": message
                        }
                    )
                    
                    if response.status_code == 200:
                        logger.info(f"Auto message sent to user {user['user_tg_id']}: {message[:50]}...")
                        update_last_auto_message(user["user_tg_id"])
                        
                        # Сохраняем сообщение в БД
                        save_message(user["chat_id"], user["user_tg_id"], "assistant", message, None)
                    else:
                        logger.warning(f"Failed to send auto message to user {user['user_tg_id']}: {response.text}")
                        
            except Exception as e:
                logger.exception(f"Error sending auto message to user {user['user_tg_id']}: {e}")
                
    except Exception as e:
        logger.exception(f"Error in send_auto_messages: {e}")

async def auto_message_scheduler():
    """Планировщик автоматических сообщений - каждые 2 часа"""
    while True:
        try:
            await send_auto_messages()
            await asyncio.sleep(2 * 60 * 60)  # 2 часа
        except Exception as e:
            logger.exception(f"Error in auto_message_scheduler: {e}")
            await asyncio.sleep(60)  # При ошибке ждем минуту

# -----------------------------
# Telegram Application
# -----------------------------
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is empty (webhook/бот работать не будет)")

async def ping_scheduler():
    """Ping-бот для предотвращения засыпания приложения на Render"""
    while True:
        try:
            # Отправляем ping каждые 10 минут
            await asyncio.sleep(600)  # 10 минут
            
            # Логируем ping
            logger.info("🔄 Ping: keeping app alive")
            
            # Можно добавить HTTP запрос к самому себе
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.get("https://drinking-buddy-bot.onrender.com/")
                    logger.info(f"✅ Self-ping successful: {response.status_code}")
                except Exception as e:
                    logger.warning(f"⚠️ Self-ping failed: {e}")
                    
        except Exception as e:
            logger.exception(f"❌ Ping scheduler error: {e}")
            await asyncio.sleep(60)  # Ждем минуту перед повтором

def build_application() -> Application:
    """Создает и настраивает приложение"""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app_ = Application.builder().token(BOT_TOKEN).build()

    # Добавляем обработчики
    app_.add_handler(CommandHandler("gift", gift_command))
    app_.add_handler(CallbackQueryHandler(gift_callback, pattern="^(gift_|gift_menu)"))
    app_.add_handler(PreCheckoutQueryHandler(pre_checkout_query))
    app_.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app_.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    
    return app_

# -----------------------------
# Утилиты БД
# -----------------------------
U = DB_FIELDS["users"]  # короткий алиас
M = DB_FIELDS["messages"]  # короткий алиас

def upsert_user_from_update(update: Update) -> None:
    """Обновление/создание пользователя из Telegram Update"""
    if not update.message or not update.message.from_user:
        return

    tg_id = update.message.from_user.id
    chat_id = update.message.chat_id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    last_name = update.message.from_user.last_name

    with engine.begin() as conn:
        # Проверяем существует ли пользователь по PRIMARY KEY (tg_id)
        existing = conn.execute(
            text(f"SELECT tg_id FROM {USERS_TABLE} WHERE tg_id = :tg_id"),
            {"tg_id": tg_id},
        ).fetchone()

        if existing:
            # Обновляем существующего пользователя
            conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE}
                    SET {U['username']} = :username, {U['first_name']} = :first_name, {U['last_name']} = :last_name, {U['chat_id']} = :chat_id, {U['user_tg_id']} = :tg_id
                    WHERE tg_id = :tg_id
                """),
                {
                    "tg_id": tg_id,
                    "chat_id": chat_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )
        else:
            # Создаем нового пользователя
            conn.execute(
                text(f"""
                    INSERT INTO {USERS_TABLE} ({U['user_tg_id']}, {U['chat_id']}, {U['username']}, {U['first_name']}, {U['last_name']}, tg_id)
                    VALUES (:tg_id, :chat_id, :username, :first_name, :last_name, :tg_id)
                """),
                {
                    "tg_id": tg_id,
                    "chat_id": chat_id,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )

def save_message(chat_id: int, user_tg_id: int, role: str, content: str, message_id: Optional[int] = None, reply_to_message_id: Optional[int] = None, sticker_sent: Optional[str] = None) -> None:
    """Сохранение сообщения в БД"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                INSERT INTO {MESSAGES_TABLE} ({M['chat_id']}, {M['user_tg_id']}, {M['role']}, {M['content']}, {M['message_id']}, {M['reply_to_message_id']}, sticker_sent)
                VALUES (:chat_id, :user_tg_id, :role, :content, :message_id, :reply_to_message_id, :sticker_sent)
            """),
            {
                "chat_id": chat_id,
                "user_tg_id": user_tg_id,
                "role": role,
                "content": content,
                "message_id": message_id,
                "reply_to_message_id": reply_to_message_id,
                "sticker_sent": sticker_sent,
            },
        )

def get_user_age(user_tg_id: int) -> Optional[int]:
    """Получение возраста пользователя из БД"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {U['age']} FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        age = row[0] if row and row[0] else None
        logger.info(f"Retrieved age for user {user_tg_id}: {age}")
        return age

def get_user_name(user_tg_id: int) -> Optional[str]:
    """Получение имени пользователя из БД"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {U['first_name']} FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        name = row[0] if row and row[0] else None
        logger.info(f"Retrieved name for user {user_tg_id}: {name}")
        return name

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
        logger.info(f"Updated age for user {user_tg_id} to {age}")

def parse_age_from_text(text: str) -> Optional[int]:
    """Парсинг возраста из текста сообщения"""
    if not text:
        return None
    
    # Паттерны для поиска возраста
    patterns = [
        r'мне\s+(\d+)\s+лет',
        r'мне\s+(\d+)',
        r'(\d+)\s+лет',
        r'возраст\s+(\d+)',
        r'(\d+)\s+годиков',
        r'(\d+)\s+год',
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            age = int(match.group(1))
            # Проверяем разумные пределы возраста
            if 1 <= age <= 120:
                logger.info(f"Parsed age from text: {age}")
                return age
    
    return None

def get_recent_messages(chat_id: int, limit: int = 12) -> list:
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

def get_user_facts(user_tg_id: int) -> str:
    """Получить важные факты о пользователе для контекста"""
    facts = []
    
    # Возраст
    age = get_user_age(user_tg_id)
    if age:
        facts.append(f"Возраст: {age} лет")
    
    # Предпочтения
    preferences = get_user_preferences(user_tg_id)
    if preferences:
        facts.append(f"Любимый напиток: {preferences}")
    
    # Имя
    name = get_user_name(user_tg_id)
    if name:
        facts.append(f"Имя: {name}")
    
    return ", ".join(facts) if facts else ""

def build_conversation_context(recent_messages: list, user_text: str) -> list:
    """Построить контекст разговора с умным сжатием"""
    # Берем последние 8 сообщений (4 пары вопрос-ответ)
    context_messages = recent_messages[-8:] if len(recent_messages) > 8 else recent_messages
    
    # Разворачиваем в хронологическом порядке
    context_messages.reverse()
    
    # Фильтруем только релевантные сообщения
    filtered_messages = []
    for msg in context_messages:
        # Пропускаем очень короткие сообщения
        if len(msg["content"].strip()) < 3:
            continue
        filtered_messages.append(msg)
    
    return filtered_messages

@safe_execute
async def llm_reply(user_text: str, username: Optional[str], user_tg_id: int, chat_id: int) -> tuple[str, Optional[str]]:
    """Генерирует ответ LLM с защитой от ошибок"""
    # Валидация входных данных
    if not validate_user_input(user_text):
        logger.warning(f"Invalid user input from {user_tg_id}")
        return "Извини, не могу обработать это сообщение. Попробуй написать по-другому! 😅", None
    
    if not validate_user_id(user_tg_id) or not validate_chat_id(chat_id):
        logger.warning(f"Invalid user_id or chat_id: {user_tg_id}, {chat_id}")
        return "Произошла ошибка с данными. Попробуй еще раз! 😅", None
    
    if client is None:
        return FALLBACK_OPENAI_UNAVAILABLE, None
    
    try:
        # Получаем возраст пользователя
        user_age = get_user_age(user_tg_id)
        
        # Получаем историю сообщений (увеличили лимит)
        recent_messages = get_recent_messages(chat_id, limit=12)
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        
        # Получаем предпочтения пользователя
        user_preferences = get_user_preferences(user_tg_id)
        logger.info(f"Retrieved preferences for user {user_tg_id}: {user_preferences}")
        
        # Проверяем, нужно ли спрашивать о предпочтениях
        should_ask_prefs = should_ask_preferences(user_tg_id)
        logger.info(f"Should ask preferences for user {user_tg_id}: {should_ask_prefs}")
        
        # Добавляем важные факты о пользователе
        user_facts = get_user_facts(user_tg_id)
        if user_facts:
            messages.append({"role": "system", "content": f"ВАЖНЫЕ ФАКТЫ О ПОЛЬЗОВАТЕЛЕ: {user_facts}. Всегда помни эти факты и используй их в разговоре."})
            logger.info(f"Added user facts: {user_facts}")
        
        # Добавляем информацию о предпочтениях
        if user_preferences:
            messages.append({"role": "system", "content": f"Предпочтения пользователя в напитках: {user_preferences}. НЕ спрашивай о предпочтениях, используй эту информацию."})
            logger.info(f"Added preferences to LLM prompt: {user_preferences}")
        elif should_ask_prefs:
            messages.append({"role": "system", "content": "Можешь спросить о предпочтениях в напитках, но только один раз в этом разговоре."})
            logger.info(f"Added preference question prompt to LLM")
            # Обновляем дату последнего вопроса
            update_last_preference_ask(user_tg_id)
        
        # Проверяем игровой контекст
        active_game = detect_game_context(recent_messages)
        if active_game:
            game_prompt = get_game_context_prompt(active_game)
            messages.append({"role": "system", "content": game_prompt})
            logger.info(f"Added game context: {active_game}")
        
        # Проверяем праздники
        today_holidays = get_today_holidays()
        if today_holidays and should_suggest_holiday(user_tg_id):
            holiday_text = ", ".join(today_holidays)
            messages.append({"role": "system", "content": f"СЕГОДНЯ ПРАЗДНИК: {holiday_text}! Можешь предложить выпить в честь этого праздника, но только один раз сегодня."})
            logger.info(f"Added holiday context: {holiday_text}")
            update_last_holiday_suggest(user_tg_id)
        
        # Добавляем контекст разговора (теперь включаем сообщения пользователя!)
        conversation_context = build_conversation_context(recent_messages, user_text)
        for msg in conversation_context:
            messages.append(msg)
        
        messages.append({"role": "user", "content": user_text})
        
        logger.info(f"LLM request for user {user_tg_id}: {len(messages)} messages, context: {len(conversation_context)} messages")

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.3,  # Уменьшаем температуру для более послушных ответов
            max_tokens=250,
        )
        
        response_text = resp.choices[0].message.content.strip()
        
        # Логируем полный ответ LLM для диагностики
        logger.info(f"LLM raw response for user {user_tg_id}: '{response_text}'")
        
        # АВТОМАТИЧЕСКОЕ ОПРЕДЕЛЕНИЕ СТИКЕРА ПО КОНТЕКСТУ
        sticker_command = None
        
        # Проверяем на ключевые слова для стикеров
        response_lower = response_text.lower()
        
        # Стикеры алкоголя
        if any(keyword in response_lower for keyword in ["🍺", "пиво", "beer", "пей", "выпей", "наливай"]):
            sticker_command = "[SEND_DRINK_BEER]"
        elif any(keyword in response_lower for keyword in ["🍷", "вино", "wine", "винца", "винцо"]):
            sticker_command = "[SEND_DRINK_WINE]"
        elif any(keyword in response_lower for keyword in ["🍸", "водка", "vodka", "водочка"]):
            sticker_command = "[SEND_DRINK_VODKA]"
        elif any(keyword in response_lower for keyword in ["🥃", "виски", "whisky", "вискарь"]):
            sticker_command = "[SEND_DRINK_WHISKY]"
        elif any(keyword in response_lower for keyword in ["🍾", "шампанское", "champagne"]):
            sticker_command = "[SEND_DRINK_CHAMPAGNE]"
        
        # Стикеры Кати
        elif any(keyword in response_lower for keyword in ["😘", "💋", "целую", "поцелуй"]):
            sticker_command = "[SEND_KATYA_KISS]"
        elif any(keyword in response_lower for keyword in ["😄", "😂", "смеюсь", "весело"]):
            sticker_command = "[SEND_KATYA_LAUGH]"
        elif any(keyword in response_lower for keyword in ["😊", "рада", "счастлива", "улыбка"]):
            sticker_command = "[SEND_KATYA_HAPPY]"
        
        if sticker_command:
            logger.info(f"Auto-detected sticker: {sticker_command} for user {user_tg_id}")
        
        return response_text, sticker_command
        
    except Exception as e:
        logger.exception(f"LLM error for user {user_tg_id}: {e}")
        return "У меня сейчас проблемы с ответом. Попробуй позже! 😅", None

async def send_sticker_by_command(chat_id: int, sticker_command: str) -> None:
    """Отправка стикера по команде от LLM"""
    sticker_map = {
        "[SEND_DRINK_VODKA]": STICKERS["DRINK_VODKA"],
        "[SEND_DRINK_WHISKY]": STICKERS["DRINK_WHISKY"], 
        "[SEND_DRINK_WINE]": STICKERS["DRINK_WINE"],
        "[SEND_DRINK_BEER]": STICKERS["DRINK_BEER"],
        "[SEND_KATYA_HAPPY]": STICKERS["KATYA_HAPPY"],
        "[SEND_KATYA_SAD]": STICKERS["KATYA_SAD"],
    }
    
    sticker_id = sticker_map.get(sticker_command)
    if not sticker_id:
        logger.warning(f"Unknown sticker command: {sticker_command}")
        return
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker",
                json={
                    "chat_id": chat_id,
                    "sticker": sticker_id,
                },
            )
            response.raise_for_status()
            logger.info(f"Sent sticker {sticker_command} to chat {chat_id}")
            
            # Проверяем, нужно ли просить подарок после алкогольного стикера
            if sticker_command.startswith("[SEND_DRINK_"):
                logger.info(f"Alcohol sticker sent, checking gift request for chat {chat_id}")
                # Получаем user_tg_id из последнего сообщения
                user_tg_id = get_last_user_tg_id(chat_id)
                logger.info(f"Last user_tg_id for chat {chat_id}: {user_tg_id}")
                
                if user_tg_id:
                    alcohol_count = get_alcohol_sticker_count(user_tg_id)
                    logger.info(f"Alcohol sticker count for user {user_tg_id}: {alcohol_count}")
                    
                    if should_ask_for_gift(user_tg_id):
                        logger.info(f"Should ask for gift! Sending request to chat {chat_id}")
                        await send_gift_request(chat_id, user_tg_id)
                    else:
                        logger.info(f"Not time to ask for gift yet for user {user_tg_id}")
                else:
                    logger.warning(f"No user_tg_id found for chat {chat_id}")
                    
        except Exception as e:
            logger.exception(f"Failed to send sticker {sticker_command}: {e}")

async def gift_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /gift - покупка алкоголя за звезды"""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    user_tg_id = update.message.from_user.id
    
    logger.info(f"Gift command received from user {user_tg_id}")
    
    # Создаем инлайн клавиатуру с вариантами напитков
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [
            InlineKeyboardButton("🍷 Вино (250 ⭐)", callback_data="gift_wine"),
            InlineKeyboardButton("🍸 Водка (100 ⭐)", callback_data="gift_vodka")
        ],
        [
            InlineKeyboardButton("🥃 Виски (500 ⭐)", callback_data="gift_whisky"),
            InlineKeyboardButton("🍺 Пиво (50 ⭐)", callback_data="gift_beer")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎁 Выбери напиток для Кати:\n\n"
        "Катя будет очень рада получить от тебя подарок! 💕",
        reply_markup=reply_markup
    )

async def gift_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на кнопки подарков"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    logger.info(f"Gift callback from user {user_id}: {data}")
    
    if data == "gift_menu":
        await show_gift_menu(query)
        return
    
    # Информация о напитках
    drink_info = {
        "gift_wine": {"name": "🍷 Вино", "stars": 250, "sticker": "[SEND_DRINK_WINE]"},
        "gift_vodka": {"name": "🍸 Водка", "stars": 100, "sticker": "[SEND_DRINK_VODKA]"},
        "gift_whisky": {"name": "🥃 Виски", "stars": 500, "sticker": "[SEND_DRINK_WHISKY]"},
        "gift_beer": {"name": "🍺 Пиво", "stars": 50, "sticker": "[SEND_DRINK_BEER]"}
    }
    
    if data not in drink_info:
        await query.edit_message_text("❌ Неизвестный напиток")
        return
    
    drink = drink_info[data]
    
    # Создаем ПЛАТЕЖНОЕ сообщение через send_invoice
    from telegram import LabeledPrice
    
    try:
        await query.message.reply_invoice(
            title=f"🎁 Подарок для Кати: {drink['name']}",
            description=f"Катя будет в восторге от этого подарка! 💕",
            payload=f"gift_{data}",  # Уникальный payload для идентификации
            provider_token="",  # Для Telegram Stars не нужен
            currency="XTR",  # Telegram Stars
            prices=[LabeledPrice(f"{drink['name']}", drink['stars'])],
            start_parameter=f"gift_{data}",
            photo_url="https://via.placeholder.com/300x200/FF6B6B/FFFFFF?text=🎁+Gift+for+Katya",
            photo_width=300,
            photo_height=200
        )
        
        # Удаляем старое сообщение с меню
        try:
            await query.message.delete()
        except Exception as e:
            logger.exception(f"Failed to delete old message: {e}")
            
    except Exception as e:
        logger.exception(f"Failed to send invoice: {e}")
        await query.edit_message_text("❌ Ошибка при создании платежа")

async def show_gift_menu(query) -> None:
    """Показывает меню подарков"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [
            InlineKeyboardButton("🍷 Вино (250 ⭐)", callback_data="gift_wine"),
            InlineKeyboardButton("🍸 Водка (100 ⭐)", callback_data="gift_vodka")
        ],
        [
            InlineKeyboardButton("🥃 Виски (500 ⭐)", callback_data="gift_whisky"),
            InlineKeyboardButton("🍺 Пиво (50 ⭐)", callback_data="gift_beer")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎁 Выбери напиток для Кати:\n\n"
        "Катя будет очень рада получить от тебя подарок! 💕",
        reply_markup=reply_markup
    )

async def pre_checkout_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик предварительной проверки платежа"""
    query = update.pre_checkout_query
    if not query:
        return
    
    logger.info(f"Pre-checkout query: {query.invoice_payload}")
    
    # Подтверждаем платеж
    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик успешного платежа"""
    if not update.message or not update.message.successful_payment:
        return
    
    payment = update.message.successful_payment
    user_tg_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    logger.info(f"Successful payment from user {user_tg_id}: {payment.invoice_payload}")
    
    # Определяем какой напиток был куплен
    drink_info = {
        "wine": {"name": "🍷 Вино", "sticker": "[SEND_DRINK_WINE]", "emoji": "🍷"},
        "vodka": {"name": "🍸 Водка", "sticker": "[SEND_DRINK_VODKA]", "emoji": "🍸"},
        "whisky": {"name": "🥃 Виски", "sticker": "[SEND_DRINK_WHISKY]", "emoji": "🥃"},
        "beer": {"name": "🍺 Пиво", "sticker": "[SEND_DRINK_BEER]", "emoji": "🍺"}
    }
    
    drink_type = payment.invoice_payload
    if drink_type not in drink_info:
        drink_type = "wine"  # fallback
    
    drink = drink_info[drink_type]
    
    # Отправляем искреннюю благодарность
    gratitude_messages = [
        f"🎉 Ого! Ты подарил мне {drink['name']}!",
        f"💕 Я так рада! Спасибо тебе огромное!",
        f" Ты самый лучший! Сейчас выпью твой подарок!",
        f"{drink['emoji']} *выпивает* Ммм, как вкусно!",
        f"💖 Ты сделал мой день! Обнимаю тебя! 🤗"
    ]
    
    for i, message in enumerate(gratitude_messages):
        try:
            await update.message.reply_text(message)
            if i == 0:  # После первого сообщения отправляем стикер
                await send_sticker_by_command(chat_id, drink['sticker'])
        except Exception as e:
            logger.exception(f"Failed to send gratitude message {i}: {e}")
    
    # Сохраняем сообщения благодарности
    try:
        for message in gratitude_messages:
            save_message(chat_id, user_tg_id, "assistant", message)
    except Exception as e:
        logger.exception(f"Failed to save gratitude messages: {e}")

# -----------------------------
# Хендлер сообщений
# -----------------------------
@safe_execute
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик сообщений с защитой"""
    if not update.message or not update.message.text:
        return

    text_in = update.message.text
    chat_id = update.message.chat_id
    user_tg_id = update.message.from_user.id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    message_id = update.message.message_id

    logger.info("Received message: %s from user %s", text_in, user_tg_id)

    # Rate limiting
    if not check_rate_limit(user_tg_id):
        await update.message.reply_text("Слишком много сообщений! Подожди минуту 😅")
        return
    
    # Валидация
    if not validate_user_input(text_in):
        await update.message.reply_text("Извини, не могу обработать это сообщение 😅")
        return
    
    if not validate_user_id(user_tg_id) or not validate_chat_id(chat_id):
        logger.warning(f"Invalid IDs: user={user_tg_id}, chat={chat_id}")
        return

    # 1) Обновляем/создаем пользователя
    try:
        upsert_user_from_update(update)
    except Exception:
        logger.exception("Failed to upsert user")

    # 2) Сохраняем сообщение пользователя
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

    # 3.5) Проверяем на упоминание предпочтений напитков
    preferences = parse_drink_preferences(text_in)
    if preferences:
        try:
            update_user_preferences(user_tg_id, preferences)
            logger.info("Updated user preferences to %s", preferences)
        except Exception:
            logger.exception("Failed to update user preferences")

    # 4) Генерируем ответ через OpenAI
    answer, sticker_command = await llm_reply(text_in, username, user_tg_id, chat_id)

    # 5) Отправляем ответ
    try:
        sent_message = await update.message.reply_text(answer)
        
        # 6) Проверяем, можем ли отправить стикер (если LLM его определил)
        if sticker_command:
            # Проверяем, может ли Катя пить бесплатно
            if can_katya_drink_free(chat_id):
                # Отправляем стикер и увеличиваем счетчик
                await send_sticker_by_command(sticker_command, chat_id)
                increment_katya_drinks(chat_id)
                
                # Сохраняем ответ бота С информацией о стикере
                save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id, None, sticker_command)
            else:
                # Катя исчерпала лимит бесплатных напитков - НЕ отправляем стикер
                await send_gift_request(chat_id, user_tg_id)
                
                # Сохраняем ответ бота БЕЗ стикера
                save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id)
        else:
            # Сохраняем ответ бота без стикера
            save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id)
    except Exception as e:
        logger.exception(f"Message handler error: {e}")
        try:
            await update.message.reply_text("Произошла ошибка. Попробуй еще раз! 🤔")
        except Exception:
            pass  # Не падаем из-за ошибки в ответе

def get_alcohol_sticker_count(user_tg_id: int) -> int:
    """Получение количества стикеров алкоголя для пользователя"""
    with engine.begin() as conn:
        # Подсчитываем сообщения с алкогольными стикерами за последние 24 часа
        result = conn.execute(
            text(f"""
                SELECT COUNT(*) FROM {MESSAGES_TABLE} 
                WHERE {M['user_tg_id']} = :user_tg_id 
                AND {M['role']} = 'assistant'
                AND sticker_sent LIKE '[SEND_DRINK_%'
                AND {M['created_at']} > NOW() - INTERVAL '24 hours'
            """),
            {"user_tg_id": user_tg_id}
        ).fetchone()
        return result[0] if result else 0

def should_ask_for_gift(user_tg_id: int) -> bool:
    """Проверяет, нужно ли просить подарок (каждые 3 стикера алкоголя)"""
    count = get_alcohol_sticker_count(user_tg_id)
    should_ask = count > 0 and count % 3 == 0
    logger.info(f"Checking gift request for user {user_tg_id}: count={count}, should_ask={should_ask}")
    return should_ask

async def send_gift_request(chat_id: int, user_tg_id: int) -> None:
    """Отправить запрос на подарок когда у Кати закончились бесплатные напитки"""
    messages = [
        "Я бы хотела с тобой выпить, но у меня на сегодня закончились напитки 😔",
        "Можешь купить мне что-нибудь выпить? 🍷"
    ]
    
    # Отправляем сообщение с кнопкой через Telegram API
    async with httpx.AsyncClient() as client:
        try:
            # Конвертируем InlineKeyboardMarkup в словарь
            keyboard_dict = {
                "inline_keyboard": [
                    [{"text": "🎁 Купить подарок Кате", "callback_data": "gift_menu"}]
                ]
            }
            
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "\n".join(messages),
                    "reply_markup": keyboard_dict
                }
            )
            response.raise_for_status()
            logger.info(f"Sent gift request to chat {chat_id}")
        except Exception as e:
            logger.exception(f"Failed to send gift request: {e}")

def get_last_user_tg_id(chat_id: int) -> Optional[int]:
    """Получение user_tg_id последнего пользователя в чате"""
    with engine.begin() as conn:
        result = conn.execute(
            text(f"""
                SELECT {M['user_tg_id']} FROM {MESSAGES_TABLE} 
                WHERE {M['chat_id']} = :chat_id 
                ORDER BY {M['created_at']} DESC 
                LIMIT 1
            """),
            {"chat_id": chat_id}
        ).fetchone()
        return result[0] if result else None

def get_user_preferences(user_tg_id: int) -> Optional[str]:
    """Получить предпочтения пользователя"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT preferences FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        return row[0] if row and row[0] else None

def update_user_preferences(user_tg_id: int, preferences: str) -> None:
    """Обновить предпочтения пользователя"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET preferences = :preferences
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id, "preferences": preferences},
        )

def get_last_preference_ask(user_tg_id: int) -> Optional[str]:
    """Получить дату последнего вопроса о предпочтениях"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT last_preference_ask FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        return row[0] if row and row[0] else None

def update_last_preference_ask(user_tg_id: int) -> None:
    """Обновить дату последнего вопроса о предпочтениях"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET last_preference_ask = CURRENT_DATE
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id},
        )

def should_ask_preferences(user_tg_id: int) -> bool:
    """Проверить, нужно ли спрашивать о предпочтениях (максимум раз в сутки)"""
    with engine.begin() as conn:
        result = conn.execute(
            text(f"""
                SELECT CASE 
                    WHEN last_preference_ask IS NULL OR last_preference_ask < CURRENT_DATE THEN true 
                    ELSE false 
                END
                FROM {USERS_TABLE} 
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id}
        ).fetchone()
        return result[0] if result else True

def parse_drink_preferences(text: str) -> Optional[str]:
    """Парсить предпочтения напитков из текста"""
    text_lower = text.lower()
    
    # Паттерны для извлечения полных названий напитков (только явные команды)
    patterns = [
        r"запомни\s+что\s+мое\s+любимое\s+пиво\s+это\s+([^.!?]+)",
        r"запомни\s+что\s+мой\s+любимый\s+напиток\s+это\s+([^.!?]+)",
        r"мое\s+любимое\s+пиво\s+это\s+([^.!?]+)",
        r"мой\s+любимый\s+напиток\s+это\s+([^.!?]+)",
        r"предпочитаю\s+([^.!?]+)",
        r"мне\s+нравится\s+([^.!?]+)",
        r"пью\s+([^.!?]+)",
        r"запоминай?\s*[:\-]?\s*([^.!?]+)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            drink_name = match.group(1).strip()
            # Очищаем от лишних слов
            drink_name = re.sub(r'\b(это|эта|этот|мое|мой|моя|мне|мне|мне)\b', '', drink_name).strip()
            if drink_name and len(drink_name) > 2:
                return drink_name
    
    # Если не нашли по паттернам, НЕ ищем ключевые слова для вопросов
    # Ключевые слова только для явных утверждений
    if any(word in text_lower for word in ["запомни", "предпочитаю", "люблю", "пью", "нравится"]):
        drinks = {
            "пиво": ["пиво", "пивко", "пивка", "🍺"],
            "вино": ["вино", "винца", "винцо", "🍷"],
            "водка": ["водка", "водочка", "🍸"],
            "виски": ["виски", "вискарь", "🥃"],
            "шампанское": ["шампанское", "🍾"],
            "коньяк": ["коньяк", "коньячок"],
            "ром": ["ром", "ромчик"],
            "джин": ["джин", "джинчик"]
        }
        
        found_drinks = []
        for drink_name, keywords in drinks.items():
            if any(keyword in text_lower for keyword in keywords):
                found_drinks.append(drink_name)
        
        if found_drinks:
            return ", ".join(found_drinks)
    
    return None

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
@safe_execute  # Исправлено: используем async декоратор
async def telegram_webhook(token: str, request: Request):
    """Webhook с защитой от ошибок"""
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
        
        # Валидация данных webhook
        if not isinstance(data, dict):
            logger.warning("Invalid webhook data format")
            return PlainTextResponse("Invalid data", status_code=400)
        
        if tapp is None:
            logger.error("Telegram application not initialized")
            return PlainTextResponse("Service unavailable", status_code=503)
        
        update = Update.de_json(data, tapp.bot)
        await tapp.process_update(update)
        return PlainTextResponse("OK")
        
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return PlainTextResponse("Internal error", status_code=500)

# Добавляем endpoint для мониторинга
@app.get("/health")
async def health_check():
    """Endpoint для проверки здоровья приложения"""
    return get_health_status()

@app.get("/metrics")
async def get_metrics():
    """Endpoint для метрик (можно использовать с Prometheus)"""
    return {
        "error_counts": dict(error_counts),
        "rate_limits": {str(k): v for k, v in user_message_counts.items()},
        "uptime": time.time() - start_time
    }

# -----------------------------
# События запуска/остановки
# -----------------------------
@app.on_event("startup")
async def on_startup():
    """Инициализация при запуске"""
    global tapp
    logger.info("🚀 Starting application...")
    
    # Инициализация БД
    init_db()
    
    # Инициализация Telegram приложения
    tapp = build_application()
    await tapp.initialize()
    await tapp.start()
    
    # Установка webhook
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"
    await tapp.bot.set_webhook(webhook_url)
    logger.info(f"✅ Webhook set to {webhook_url}")
    
    # Запуск планировщиков
    asyncio.create_task(ping_scheduler())
    asyncio.create_task(auto_message_scheduler())
    logger.info("✅ Auto message scheduler started")

@app.on_event("shutdown")
async def on_shutdown():
    if tapp:
        try:
            await tapp.stop()
        except Exception:
            logger.exception("Error on telegram app stop")

# -----------------------------
# Система безопасности и мониторинга
# -----------------------------

# Счетчики ошибок для мониторинга
error_counts = defaultdict(int)
last_error_time = defaultdict(float)

def safe_execute(func):
    """Декоратор для безопасного выполнения функций"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            error_counts[func.__name__] += 1
            last_error_time[func.__name__] = time.time()
            logger.exception(f"Critical error in {func.__name__}: {e}")
            
            # Отправляем уведомление о критической ошибке
            await notify_critical_error(func.__name__, str(e))
            
            # Возвращаем безопасный fallback
            return get_fallback_response(func.__name__)
    return wrapper

def safe_execute_sync(func):
    """Декоратор для безопасного выполнения синхронных функций"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_counts[func.__name__] += 1
            last_error_time[func.__name__] = time.time()
            logger.exception(f"Critical error in {func.__name__}: {e}")
            return get_fallback_response(func.__name__)
    return wrapper

async def notify_critical_error(function_name: str, error: str) -> None:
    """Уведомление о критической ошибке (можно отправить в мониторинг)"""
    try:
        logger.critical(f"CRITICAL ERROR in {function_name}: {error}")
        # Здесь можно добавить отправку в Slack, Telegram, Sentry и т.д.
    except Exception:
        pass  # Не падаем из-за ошибки в уведомлении

def get_fallback_response(function_name: str) -> str:
    """Безопасный fallback ответ"""
    fallbacks = {
        "llm_reply": "Извини, у меня сейчас технические проблемы. Попробуй позже! 😅",
        "msg_handler": None,  # Не отправляем ответ при ошибке в обработчике
        "send_auto_messages": None,  # Пропускаем автоматические сообщения
        "ping_scheduler": None,  # Пропускаем ping
    }
    return fallbacks.get(function_name, "Что-то пошло не так. Попробуй еще раз! 🤔")

def validate_user_input(text: str) -> bool:
    """Валидация пользовательского ввода"""
    if not text or not isinstance(text, str):
        return False
    
    # Проверяем длину сообщения
    if len(text) > 4000:  # Telegram лимит
        return False
    
    # Проверяем на подозрительные паттерны
    suspicious_patterns = [
        r'<script.*?>',
        r'javascript:',
        r'data:',
        r'vbscript:',
        r'onload=',
        r'onerror=',
    ]
    
    text_lower = text.lower()
    for pattern in suspicious_patterns:
        if re.search(pattern, text_lower):
            logger.warning(f"Suspicious input detected: {pattern}")
            return False
    
    return True

def validate_user_id(user_id: int) -> bool:
    """Валидация ID пользователя"""
    if not isinstance(user_id, int):
        return False
    
    # Telegram ID должен быть положительным числом
    if user_id <= 0:
        return False
    
    # Проверяем разумные пределы
    if user_id > 999999999999:  # Максимальный Telegram ID
        return False
    
    return True

def validate_chat_id(chat_id: int) -> bool:
    """Валидация ID чата"""
    if not isinstance(chat_id, int):
        return False
    
    # Чат ID может быть отрицательным (группы)
    if abs(chat_id) > 999999999999:
        return False
    
    return True

# Rate limiting
user_message_counts = defaultdict(int)
user_last_message = defaultdict(float)
RATE_LIMIT_MESSAGES = 10  # Максимум сообщений в минуту
RATE_LIMIT_WINDOW = 60  # Окно в секундах

def check_rate_limit(user_id: int) -> bool:
    """Проверка rate limit для пользователя"""
    current_time = time.time()
    
    # Сбрасываем счетчик если прошла минута
    if current_time - user_last_message[user_id] > RATE_LIMIT_WINDOW:
        user_message_counts[user_id] = 0
        user_last_message[user_id] = current_time
    
    # Проверяем лимит
    if user_message_counts[user_id] >= RATE_LIMIT_MESSAGES:
        logger.warning(f"Rate limit exceeded for user {user_id}")
        return False
    
    user_message_counts[user_id] += 1
    return True

def get_health_status() -> dict:
    """Получение статуса здоровья приложения"""
    return {
        "status": "healthy",
        "errors": dict(error_counts),
        "database": "connected" if engine else "disconnected",
        "telegram": "connected" if tapp else "disconnected",
        "openai": "connected" if client else "disconnected",
        "uptime": time.time() - start_time if 'start_time' in globals() else 0
    }

# Глобальная переменная для отслеживания времени запуска
start_time = time.time()

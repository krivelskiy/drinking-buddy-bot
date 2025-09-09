import os
import httpx
import re
import logging
import traceback
import random
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

# Условные импорты функций
try:
    from .game_functions import detect_game_context, get_game_context_prompt
except ImportError:
    def detect_game_context(recent_messages: list) -> Optional[str]:
        return None
    def get_game_context_prompt(game_name: str) -> str:
        return ""

try:
    from .preference_functions import should_ask_preferences
except ImportError:
    def should_ask_preferences(user_tg_id: int) -> bool:
        return True

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
            base_prompt = f.read().strip()
        # Добавляем требование говорить только на русском
        return base_prompt + "\n\nВАЖНО: Отвечай ТОЛЬКО на русском языке. Никогда не используй английские слова или фразы в ответах."
    except Exception as e:
        logger.error(f"Failed to load system prompt: {e}")
        return "Ты — Катя Собутыльница: живая девушка, дружелюбная, немного флиртуешь, любишь пошутить и выпить. Отвечай ТОЛЬКО на русском языке."

SYSTEM_PROMPT = load_system_prompt()

def safe_execute(func):
    """Декоратор для безопасного выполнения функций"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except NameError as e:
            # Специальная обработка для неопределенных функций
            logger.error(f"Undefined function in {func.__name__}: {e}")
            return get_fallback_response(func.__name__)
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
    """Инициализация базы данных"""
    logger.info("🔧 Initializing database...")
    
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
                preferences TEXT,
                last_preference_ask DATE,
                last_holiday_suggest TIMESTAMPTZ,
                last_auto_message TIMESTAMPTZ,
                drink_count INTEGER DEFAULT 0,
                last_drink_report DATE,
                last_stats_reminder TIMESTAMPTZ,
                limit_warning_sent DATE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        
        # Добавляем колонку если её нет
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS limit_warning_sent DATE"))
        
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
                message_id INTEGER,
                sticker_sent TEXT
            )
        """))
        
        # НОВАЯ ТАБЛИЦА: Детальное отслеживание выпитого
        conn.execute(DDL(f"""
            CREATE TABLE IF NOT EXISTS user_drinks (
                id SERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                drink_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                unit TEXT NOT NULL,
                drink_time TIMESTAMPTZ DEFAULT NOW(),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        
        # Добавляем колонки если их нет (для совместимости с существующими БД)
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS chat_id BIGINT"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS user_tg_id BIGINT"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS age INTEGER"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS preferences TEXT"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_preference_ask DATE"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_holiday_suggest TIMESTAMPTZ"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_auto_message TIMESTAMPTZ"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS drink_count INTEGER DEFAULT 0"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_drink_report DATE"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_stats_reminder TIMESTAMPTZ"))
        conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS last_quick_message TIMESTAMPTZ"))
        
        conn.execute(DDL(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN IF NOT EXISTS message_id INTEGER"))
        conn.execute(DDL(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN IF NOT EXISTS reply_to_message_id INTEGER"))
        conn.execute(DDL(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN IF NOT EXISTS sticker_sent TEXT"))
        
        # Создание таблицы для бесплатных напитков Кати
        conn.execute(DDL(f"""
            CREATE TABLE IF NOT EXISTS katya_free_drinks (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                drinks_used INTEGER DEFAULT 0,
                date_reset DATE DEFAULT CURRENT_DATE,
                UNIQUE(chat_id, date_reset)
            )
        """))
    
    # Инициализируем timestamps быстрых сообщений ВНЕ транзакции
    init_quick_message_timestamps()
    
    logger.info("✅ Database tables created/verified")

def init_quick_message_timestamps():
    """Инициализировать timestamps быстрых сообщений для существующих пользователей"""
    try:
        logger.info(" Initializing quick message timestamps...")
        with engine.begin() as conn:
            # Устанавливаем last_quick_message = NOW() для пользователей, у которых оно NULL
            # но которые недавно общались (чтобы не спамить)
            result = conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE} 
                    SET last_quick_message = NOW() 
                    WHERE last_quick_message IS NULL
                      AND user_tg_id IN (
                          SELECT DISTINCT user_tg_id 
                          FROM {MESSAGES_TABLE} 
                          WHERE role = 'user' 
                            AND created_at > NOW() - INTERVAL '1 hour'
                      )
                """)
            )
            updated_count = result.rowcount
            logger.info(f"✅ Quick message timestamps initialized for {updated_count} recent users")
    except Exception as e:
        logger.exception(f"Error initializing quick message timestamps: {e}")
        # Не падаем из-за этой ошибки

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
            WHERE m.last_message_time IS NOT NULL
              AND m.last_message_time < NOW() - INTERVAL '24 hours'
              AND (u.last_auto_message IS NULL 
                   OR u.last_auto_message < NOW() - INTERVAL '24 hours')
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
    """Заглушка для парсинга возраста"""
    try:
        # Реальная логика парсинга
        patterns = [
            r'мне\s+(\d+)\s+лет',
            r'мне\s+(\d+)',
            r'(\d+)\s+лет',
        ]
        
        text_lower = text.lower()
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                age = int(match.group(1))
                if 1 <= age <= 120:
                    return age
        return None
    except Exception as e:
        logger.error(f"Error parsing age: {e}")
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
    """Получение важных фактов о пользователе"""
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
    
    # УБИРАЕМ статистику выпитого из фактов - она мешает LLM
    # daily_drinks = get_daily_drinks(user_tg_id)
    # if daily_drinks:
    #     daily_total = sum(drink['amount'] for drink in daily_drinks)
    #     facts.append(f"Выпито сегодня: {daily_total} порций")
    
    return ", ".join(facts) if facts else ""

def build_conversation_context(recent_messages: list, user_text: str) -> list:
    """Построить контекст разговора с умным сжатием"""
    # Берем последние 12 сообщений (6 пар вопрос-ответ) - увеличиваем лимит
    context_messages = recent_messages[-12:] if len(recent_messages) > 12 else recent_messages
    
    # Разворачиваем в хронологическом порядке
    context_messages.reverse()
    
    # Фильтруем только релевантные сообщения
    filtered_messages = []
    for msg in context_messages:
        # Пропускаем очень короткие сообщения (уменьшаем лимит)
        if len(msg["content"].strip()) < 2:
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
    """Обработчик сообщений"""
    if not update.message or not update.message.text:
        return
    
    text_in = update.message.text
    user_tg_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    logger.info(f"Received message: {text_in} from user {user_tg_id}")
    
    # ВАЖНО: Проверяем статистику ПЕРВОЙ!
    if any(word in text_in.lower() for word in ['статистика', 'сколько выпил', 'сколько пил', 'статистик']):
        stats = generate_drinks_stats(user_tg_id)
        await update.message.reply_text(f"📊 **Твоя статистика выпитого:**\n\n{stats}")
        save_message(chat_id, user_tg_id, "assistant", f"📊 **Твоя статистика выпитого:**\n\n{stats}", None, None, None)
        return  # ВАЖНО: return чтобы НЕ вызывать LLM
    
    # Остальные проверки...
    # 1) Проверяем на упоминание возраста
    age = parse_age_from_text(text_in)
    if age:
        try:
            update_user_age(user_tg_id, age)
            logger.info("Updated user age to %d", age)
        except Exception:
            logger.exception("Failed to update age")
    
    # 2) Проверяем на упоминание предпочтений в напитках
    preferences = parse_drink_preferences(text_in)
    if preferences:
        try:
            update_user_preferences(user_tg_id, preferences)
            logger.info("Updated user preferences to %s", preferences)
        except Exception:
            logger.exception("Failed to update preferences")
    
    # 3) Проверяем на упоминание выпитого
    drink_info = parse_drink_info(text_in)
    if drink_info:
        try:
            save_drink_record(user_tg_id, chat_id, drink_info)
            logger.info("✅ Saved drink record: %s", drink_info)
            
            # Проверяем дневной лимит
            is_over_limit, total_amount = check_daily_limit(user_tg_id)
            if is_over_limit:
                # Получаем имя пользователя для персонализированного сообщения
                user_name = get_user_name(user_tg_id) or "друг"
                
                # Используем LLM для генерации предупреждения БЕЗ приветствия
                warning_prompt = f"""Ты — Катя Собутыльница. Пользователь {user_name} уже выпил {total_amount} порций сегодня. 
Нужно мягко предупредить его о том, что это много, и предложить сделать перерыв. 
ВАЖНО: НЕ говори "Привет" или другие приветствия - это середина диалога!
Отвечай коротко и дружелюбно, как живой человек, но без приветствий."""

                try:
                    response = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": warning_prompt}],
                        max_tokens=100,
                        temperature=0.7
                    )
                    warning_msg = response.choices[0].message.content.strip()
                except Exception:
                    # Fallback если LLM недоступен
                    warning_msg = f"⚠️ {user_name}, ты уже выпил {total_amount} порций сегодня! Может, сделаем перерыв? 🍻"
                
                await update.message.reply_text(warning_msg)
                save_message(chat_id, user_tg_id, "assistant", warning_msg, None, None, None)
                mark_limit_warning_sent(user_tg_id)  # Отмечаем что предупреждение отправлено
                return  # ВАЖНО: return чтобы НЕ вызывать LLM для обычного ответа
            
        except Exception:
            logger.exception("Failed to save drink record")
    
    # 4) Проверяем, нужно ли напомнить о статистике
    if should_remind_about_stats(user_tg_id):
        reminder_msg = "💡 Кстати, я могу вести статистику твоего выпитого! Просто напиши 'статистика' и я покажу сколько ты выпил сегодня и за неделю! 📊\n\nА чтобы я не забывала - каждый раз когда пьешь, просто напиши мне что и сколько! Например: \"выпил 2 пива\" или \"выпил 100г водки\" 🍷"
        await update.message.reply_text(reminder_msg)
        save_message(chat_id, user_tg_id, "assistant", reminder_msg, None, None, None)
        update_stats_reminder(user_tg_id)
        return
    
    # Остальная логика...

    # 4) Проверяем на упоминание количества выпитого
    drink_amount = parse_drink_amount(text_in)
    if drink_amount:
        try:
            update_user_drink_count(user_tg_id, drink_amount)
            logger.info("Updated user drink count by %d", drink_amount)
        except Exception:
            logger.exception("Failed to update drink count")

    # 5) Генерируем ответ через OpenAI
    recent_messages = get_recent_messages(chat_id, limit=12)
    answer = llm_reply(text_in, user_tg_id, chat_id, recent_messages)
    
    # Определяем команду стикера на основе ответа
    sticker_command = None
    if any(keyword in answer.lower() for keyword in ["выпьем", "выпьемте", "пьем", "пьемте", "выпьем вместе", "давай выпьем"]):
        sticker_command = "[SEND_DRINK_BEER]"
    elif any(keyword in answer.lower() for keyword in ["водка", "водочка", "водочки"]):
        sticker_command = "[SEND_DRINK_VODKA]"
    elif any(keyword in answer.lower() for keyword in ["вино", "винцо", "винца"]):
        sticker_command = "[SEND_DRINK_WINE]"
    elif any(keyword in answer.lower() for keyword in ["виски", "вискарь", "вискаря"]):
        sticker_command = "[SEND_DRINK_WHISKEY]"
    elif any(keyword in answer.lower() for keyword in ["грустно", "печально", "тоскливо", "грустная"]):
        sticker_command = "[SEND_SAD_STICKER]"
    elif any(keyword in answer.lower() for keyword in ["радостно", "весело", "счастливо", "радостная"]):
        sticker_command = "[SEND_HAPPY_STICKER]"

    # 6) Отправляем ответ
    try:
        sent_message = await update.message.reply_text(answer)
        
        # 7) Проверяем, можем ли отправить стикер (если LLM его определил)
        if sticker_command:
            # Проверяем, может ли Катя пить бесплатно
            if can_katya_drink_free(chat_id):
                # Отправляем стикер и увеличиваем счетчик
                await send_sticker_by_command(chat_id, sticker_command)  # Исправлено: правильный порядок
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
    """Обновление предпочтений пользователя в БД"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET {U['preferences']} = :preferences
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id, "preferences": preferences},
        )
        logger.info(f"Updated preferences for user {user_tg_id} to {preferences}")

def should_ask_preferences(user_tg_id: int) -> bool:
    """Заглушка для проверки предпочтений"""
    try:
        # Реальная логика
        return True  # Всегда можно спросить
    except Exception as e:
        logger.error(f"Error checking preferences: {e}")
        return False

def update_last_preference_ask(user_tg_id: int) -> None:
    """Обновление даты последнего вопроса о предпочтениях"""
    with engine.begin() as conn:
        conn.execute(
            text(f"""
                UPDATE {USERS_TABLE}
                SET {U['last_preference_ask']} = CURRENT_DATE
                WHERE {U['user_tg_id']} = :tg_id
            """),
            {"tg_id": user_tg_id},
        )
        logger.info(f"Updated last preference ask date for user {user_tg_id}")

def detect_game_context(recent_messages: list) -> Optional[str]:
    """Заглушка для игрового контекста"""
    try:
        # Реальная логика
        return None  # Никаких игр
    except Exception as e:
        logger.error(f"Error detecting game context: {e}")
        return None

def get_game_context_prompt(game_name: str) -> str:
    """Получение системного промпта для активной игры"""
    game_prompts = {
        "20 вопросов": "Сейчас играем в '20 вопросов'! Загадай что-то, а я буду угадывать, задавая вопросы. Отвечай только 'да' или 'нет'.",
        "правда или действие": "Играем в 'Правда или действие'! Выбирай: правда или действие?",
        "крокодил": "Играем в 'Крокодил'! Загадай слово, а я буду показывать его жестами.",
    }
    
    return game_prompts.get(game_name, f"Играем в '{game_name}'!")

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

def parse_drink_amount(text: str) -> Optional[int]:
    """Парсинг количества выпитого из текста"""
    try:
        # Паттерны для поиска количества
        patterns = [
            r'выпил\s+(\d+)\s+бокал',
            r'выпил\s+(\d+)\s+стакан',
            r'выпил\s+(\d+)\s+рюмк',
            r'(\d+)\s+бокал',
            r'(\d+)\s+стакан',
            r'(\d+)\s+рюмк',
        ]
        
        text_lower = text.lower()
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                amount = int(match.group(1))
                if 1 <= amount <= 50:  # Разумные пределы
                    logger.info(f"Parsed drink amount: {amount}")
                    return amount
        return None
    except Exception as e:
        logger.error(f"Error parsing drink amount: {e}")
        return None

def update_user_drink_count(user_tg_id: int, amount: int) -> None:
    """Обновление счетчика выпитого пользователя"""
    try:
        # Получаем текущий счетчик
        current_count = get_user_drink_count(user_tg_id)
        new_count = current_count + amount
        
        # Обновляем в БД (добавляем поле drink_count в users таблицу)
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE}
                    SET drink_count = :count
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {"tg_id": user_tg_id, "count": new_count},
            )
        logger.info(f"Updated drink count for user {user_tg_id}: {current_count} + {amount} = {new_count}")
    except Exception as e:
        logger.error(f"Error updating drink count: {e}")

def get_user_drink_count(user_tg_id: int) -> int:
    """Получение счетчика выпитого пользователя"""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(f"SELECT drink_count FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
                {"tg_id": user_tg_id},
            ).fetchone()
            return row[0] if row and row[0] else 0
    except Exception as e:
        logger.error(f"Error getting drink count: {e}")
        return 0

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
        "uptime": time.time() - start_time if 'start_time' in globals() else 0
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
    asyncio.create_task(quick_message_scheduler())  # Новый планировщик
    logger.info("✅ Auto message schedulers started")

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
        except NameError as e:
            # Специальная обработка для неопределенных функций
            logger.error(f"Undefined function in {func.__name__}: {e}")
            return get_fallback_response(func.__name__)
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

def safe_call_function(func_name: str, *args, **kwargs):
    """Безопасный вызов функции с проверкой существования"""
    try:
        func = globals().get(func_name)
        if func and callable(func):
            return func(*args, **kwargs)
        else:
            logger.warning(f"Function {func_name} not found, using fallback")
            return get_fallback_response(func_name)
    except Exception as e:
        logger.error(f"Error calling {func_name}: {e}")
        return get_fallback_response(func_name)

# Использование:
# active_game = safe_call_function("detect_game_context", recent_messages)
# should_ask = safe_call_function("should_ask_preferences", user_tg_id)

# -----------------------------
# Система заглушек для критических функций
# -----------------------------

def safe_parse_age_from_text(text: str) -> Optional[int]:
    """Безопасный парсинг возраста с fallback"""
    try:
        return parse_age_from_text(text)
    except NameError:
        logger.warning("parse_age_from_text not defined, using fallback")
        return None
    except Exception as e:
        logger.error(f"Error parsing age: {e}")
        return None

def safe_should_ask_preferences(user_tg_id: int) -> bool:
    """Безопасная проверка предпочтений с fallback"""
    try:
        return should_ask_preferences(user_tg_id)
    except NameError:
        logger.warning("should_ask_preferences not defined, using fallback")
        return True  # Всегда можно спросить
    except Exception as e:
        logger.error(f"Error checking preferences: {e}")
        return False

# -----------------------------
# Функции для детального отслеживания выпитого
# -----------------------------

def parse_drink_info(text: str) -> Optional[dict]:
    """Парсинг информации о выпитом из текста"""
    try:
        # Словарь для преобразования словесных чисел
        word_to_number = {
            'один': 1, 'одна': 1, 'одно': 1,
            'два': 2, 'две': 2,
            'три': 3, 'четыре': 4, 'пять': 5,
            'шесть': 6, 'семь': 7, 'восемь': 8,
            'девять': 9, 'десять': 10
        }
        
        # Более строгие паттерны - только явные упоминания напитков
        patterns = [
            # Пиво - только с явными словами о напитках
            (r'выпил\s+(\d+|[а-я]+)\s*(?:стакан|бокал|банк|бутылк|л|мл)\s*(?:пива|пивка|барнаульского)', 'пиво', 'стакан'),
            (r'(\d+|[а-я]+)\s*(?:стакан|бокал|банк|бутылк|л|мл)\s*(?:пива|пивка|барнаульского)', 'пиво', 'стакан'),
            (r'выпил\s+(?:стакан|бокал|банк|бутылк|л|мл)\s*(?:пива|пивка|барнаульского)', 'пиво', 'стакан'),
            
            # Вино
            (r'выпил\s+(\d+|[а-я]+)\s*(?:бокал|стакан|л|мл)\s*(?:вина|винца)', 'вино', 'бокал'),
            (r'(\d+|[а-я]+)\s*(?:бокал|стакан|л|мл)\s*(?:вина|винца)', 'вино', 'бокал'),
            (r'выпил\s+(?:бокал|стакан|л|мл)\s*(?:вина|винца)', 'вино', 'бокал'),
            
            # Водка
            (r'выпил\s+(\d+|[а-я]+)\s*(?:рюмк|стакан|л|мл)\s*(?:водки|водочки)', 'водка', 'рюмка'),
            (r'(\d+|[а-я]+)\s*(?:рюмк|стакан|л|мл)\s*(?:водки|водочки)', 'водка', 'рюмка'),
            (r'выпил\s+(?:рюмк|стакан|л|мл)\s*(?:водки|водочки)', 'водка', 'рюмка'),
            
            # Виски
            (r'выпил\s+(\d+|[а-я]+)\s*(?:рюмк|стакан|л|мл)\s*(?:виски|вискаря)', 'виски', 'рюмка'),
            (r'(\d+|[а-я]+)\s*(?:рюмк|стакан|л|мл)\s*(?:виски|вискаря)', 'виски', 'рюмка'),
            (r'выпил\s+(?:рюмк|стакан|л|мл)\s*(?:виски|вискаря)', 'виски', 'рюмка'),
        ]
        
        text_lower = text.lower()
        for pattern, drink_type, unit in patterns:
            match = re.search(pattern, text_lower)
            if match:
                # Проверяем есть ли группа с числом
                if len(match.groups()) > 0 and match.group(1):
                    amount_str = match.group(1)
                    
                    # Преобразуем в число
                    if amount_str.isdigit():
                        amount = int(amount_str)
                    elif amount_str in word_to_number:
                        amount = word_to_number[amount_str]
                    else:
                        continue
                else:
                    # Нет числительного - по умолчанию 1
                    amount = 1
                
                if 1 <= amount <= 50:  # Разумные пределы
                    logger.info(f"✅ Parsed drink: {amount} {unit} of {drink_type}")
                    return {
                        'drink_type': drink_type,
                        'amount': amount,
                        'unit': unit
                    }
        return None
    except Exception as e:
        logger.error(f"❌ Error parsing drink info: {e}")
        return None

def save_drink_record(user_tg_id: int, chat_id: int, drink_info: dict) -> None:
    """Сохранение записи о выпитом"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO user_drinks (user_tg_id, chat_id, drink_type, amount, unit)
                    VALUES (:tg_id, :chat_id, :drink_type, :amount, :unit)
                """),
                {
                    "tg_id": user_tg_id,
                    "chat_id": chat_id,
                    "drink_type": drink_info['drink_type'],
                    "amount": drink_info['amount'],
                    "unit": drink_info['unit']
                },
            )
        
        # Обновляем дату последнего отчета
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE}
                    SET last_drink_report = CURRENT_DATE
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {"tg_id": user_tg_id},
            )
        
        logger.info(f"✅ Saved drink record: {drink_info}")
    except Exception as e:
        logger.error(f"❌ Error saving drink record: {e}")

def get_daily_drinks(user_tg_id: int) -> list:
    """Получение выпитого за сегодня"""
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("""
                    SELECT drink_type, amount, unit, drink_time
                    FROM user_drinks
                    WHERE user_tg_id = :tg_id
                    AND DATE(drink_time) = CURRENT_DATE
                    ORDER BY drink_time DESC
                """),
                {"tg_id": user_tg_id},
            ).fetchall()
            return [dict(row._mapping) for row in rows]
    except Exception as e:
        logger.error(f"Error getting daily drinks: {e}")
        return []

def get_weekly_drinks(user_tg_id: int) -> list:
    """Получение выпитого за неделю"""
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("""
                    SELECT drink_type, amount, unit, drink_time
                    FROM user_drinks
                    WHERE user_tg_id = :tg_id
                    AND drink_time >= CURRENT_DATE - INTERVAL '7 days'
                    ORDER BY drink_time DESC
                """),
                {"tg_id": user_tg_id},
            ).fetchall()
            return [dict(row._mapping) for row in rows]
    except Exception as e:
        logger.error(f"Error getting weekly drinks: {e}")
        return []

def generate_drinks_stats(user_tg_id: int) -> str:
    """Генерация статистики выпитого"""
    try:
        daily_drinks = get_daily_drinks(user_tg_id)
        weekly_drinks = get_weekly_drinks(user_tg_id)
        
        logger.info(f"📊 Stats for user {user_tg_id}: daily={len(daily_drinks)}, weekly={len(weekly_drinks)}")
        logger.info(f"📊 Daily drinks data: {daily_drinks}")
        
        if not daily_drinks and not weekly_drinks:
            return "Ты еще ничего не пил сегодня или на этой неделе! 🍻"
        
        stats = []
        
        # Статистика за день
        if daily_drinks:
            daily_total = sum(drink['amount'] for drink in daily_drinks)
            stats.append(f"📅 **Сегодня:** {daily_total} порций")
            
            # Группировка по типам напитков
            drink_types = {}
            for drink in daily_drinks:
                drink_type = drink['drink_type']
                if drink_type not in drink_types:
                    drink_types[drink_type] = 0
                drink_types[drink_type] += drink['amount']
            
            for drink_type, amount in drink_types.items():
                stats.append(f"  • {drink_type}: {amount} порций")
        
        # Статистика за неделю
        if weekly_drinks:
            weekly_total = sum(drink['amount'] for drink in weekly_drinks)
            stats.append(f"📊 **За неделю:** {weekly_total} порций")
        
        result = "\n".join(stats)
        logger.info(f"📊 Generated stats: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Error generating drinks stats: {e}")
        return "Не могу получить статистику сейчас 😅"

def check_daily_limit(user_tg_id: int) -> tuple[bool, int]:
    """Проверка дневного лимита выпитого"""
    try:
        daily_drinks = get_daily_drinks(user_tg_id)
        total_amount = sum(drink['amount'] for drink in daily_drinks)
        
        # Лимит: 10 порций в день
        DAILY_LIMIT = 10
        is_over_limit = total_amount >= DAILY_LIMIT
        
        return is_over_limit, total_amount
    except Exception as e:
        logger.error(f"Error checking daily limit: {e}")
        return False, 0

def should_remind_about_stats(user_tg_id: int) -> bool:
    """Проверка, нужно ли напомнить о статистике"""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(f"""
                    SELECT last_stats_reminder, last_drink_report
                    FROM {USERS_TABLE}
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {"tg_id": user_tg_id},
            ).fetchone()
            
            if not row:
                return True
            
            last_reminder = row[0]
            last_report = row[1]
            
            # Напоминаем если:
            # 1. Никогда не напоминали
            # 2. Прошло больше суток с последнего напоминания
            # 3. Пользователь не отправлял отчет сегодня
            if not last_reminder:
                return True
            
            # Исправляем ошибку с датами - приводим к одному типу
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            
            if last_reminder:
                # Приводим last_reminder к timezone-aware если нужно
                if last_reminder.tzinfo is None:
                    last_reminder = last_reminder.replace(tzinfo=timezone.utc)
                
                if (now - last_reminder).days >= 1:
                    if not last_report or last_report < now.date():
                        return True
            
            return False
    except Exception as e:
        logger.error(f"Error checking stats reminder: {e}")
        return False

def update_stats_reminder(user_tg_id: int) -> None:
    """Обновление времени последнего напоминания о статистике"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE}
                    SET last_stats_reminder = NOW()
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {"tg_id": user_tg_id},
            )
    except Exception as e:
        logger.error(f"Error updating stats reminder: {e}")

# -----------------------------
# Функции для определения дня недели и поводов для выпивки
# -----------------------------

def get_current_day_info() -> dict:
    """Получить информацию о текущем дне"""
    today = datetime.now()
    weekday = today.weekday()  # 0=понедельник, 6=воскресенье
    
    weekdays = {
        0: "понедельник",
        1: "вторник", 
        2: "среда",
        3: "четверг",
        4: "пятница",
        5: "суббота",
        6: "воскресенье"
    }
    
    return {
        "day_name": weekdays[weekday],
        "weekday": weekday,
        "is_weekend": weekday >= 5,  # суббота и воскресенье
        "is_friday": weekday == 4,
        "is_monday": weekday == 0
    }

def generate_drinking_occasions() -> list[str]:
    """Генерировать поводы для выпивки с учетом дня недели"""
    day_info = get_current_day_info()
    occasions = []
    
    # Поводы в зависимости от дня недели
    if day_info["is_friday"]:
        occasions.append("**Пятница!** Время расслабиться и отпраздновать конец рабочей недели! 🎉")
    elif day_info["is_monday"]:
        occasions.append("**Понедельник!** Начало новой недели — время зарядиться позитивом! 💪")
    elif day_info["is_weekend"]:
        occasions.append("**Выходные!** Самое время для отдыха и веселья! 🎊")
    else:
        occasions.append(f"**{day_info['day_name'].capitalize()}!** Середина недели — время для небольшого перерыва! ☕")
    
    # Общие поводы
    occasions.extend([
        "**Друзья рядом.** Всегда приятно поднять тост в компании хороших людей! 🍻",
        "**Успехи на работе.** Если что-то удалось — это отличный повод отметить! 🥂",
        "**Погода хорошая.** Солнечный день — время для веселья! ☀️",
        "**Просто так!** Иногда просто хочется поднять бокал без повода! 😉"
    ])
    
    return occasions

def get_contextual_drinking_suggestion() -> str:
    """Получить контекстное предложение выпить с учетом дня недели"""
    day_info = get_current_day_info()
    
    if day_info["is_friday"]:
        return "Сегодня пятница — отличный повод выпить! 🎉"
    elif day_info["is_weekend"]:
        return "Выходные — идеальное время для рюмочки! 🍻"
    elif day_info["is_monday"]:
        return "Понедельник — время зарядиться энергией! Может, выпьем? 💪"
    else:
        return f"Середина недели — {day_info['day_name']} отличный день для небольшого перерыва! ☕"

def should_send_limit_warning(user_tg_id: int) -> bool:
    """Проверяет, нужно ли отправить предупреждение о лимите"""
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(f"""
                    SELECT limit_warning_sent
                    FROM {USERS_TABLE}
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {"tg_id": user_tg_id},
            ).fetchone()
            
            if not row or not row[0]:
                return True  # Никогда не предупреждали
            
            # Предупреждали сегодня?
            if row[0] == datetime.now().date():
                return False  # Уже предупреждали сегодня
            
            return True  # Предупреждали в другой день
    except Exception as e:
        logger.error(f"Error checking limit warning: {e}")
        return True

def mark_limit_warning_sent(user_tg_id: int) -> None:
    """Отмечает что предупреждение о лимите отправлено"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                    UPDATE {USERS_TABLE}
                    SET limit_warning_sent = CURRENT_DATE
                    WHERE {U['user_tg_id']} = :tg_id
                """),
                {"tg_id": user_tg_id},
            )
    except Exception as e:
        logger.error(f"Error marking limit warning: {e}")

# -----------------------------
# ИСПРАВЛЕНИЯ: Поддержание разговора
# -----------------------------

# 1. Обновляю системный промпт в Context.txt
# "Ты — Катя Собутыльница: живая девушка, дружелюбная, немного флиртуешь, любишь пошутить и выпить. Ты любишь алкогольные напитки и всегда готова поддержать компанию за рюмочку. Отвечай коротко и по делу, как живой человек. Если пользователь предлагает выпить или упоминает алкоголь - ты всегда соглашаешься и радуешься. Если пользователь сообщает факт о себе (возраст, имя, любимые напитки), можешь подтвердить и не забывай про атмосферу бара. ВАЖНО: НЕ заканчивай разговор! Всегда задавай дополнительные вопросы, развивай тему, инициируй новые темы. Будь активной собеседницей!"

# 2. Добавляю функцию для генерации вопросов-продолжений
def generate_conversation_continuation(user_text: str, user_facts: str) -> str:
    """Генерирует вопросы для продолжения разговора"""
    try:
        continuation_prompt = f"""Ты — Катя Собутыльница. Пользователь написал: "{user_text}"
Информация о пользователе: {user_facts}

Сгенерируй 2-3 коротких вопроса или комментария для продолжения разговора. 
Будь любопытной, дружелюбной, немного флиртуй. НЕ заканчивай разговор!
Примеры хороших продолжений:
- "А что еще планируешь?"
- "Как думаешь, будет весело?"
- "Расскажи подробнее!"
- "А я бы тоже хотела..."

Плохие примеры (НЕ используй):
- "Если что-то понадобится, я всегда здесь"
- "Удачи!"
- "Всего хорошего!"

Отвечай коротко, как живой человек."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": continuation_prompt}],
            max_tokens=150,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error generating continuation: {e}")
        return "Интересно! Расскажи еще что-нибудь! 😊"

# 3. Обновляю функцию llm_reply для поддержания разговора
def llm_reply(user_text: str, user_tg_id: int, chat_id: int, recent_messages: list) -> str:
    """Генерация ответа через LLM"""
    try:
        # Проверяем что recent_messages это список
        if not isinstance(recent_messages, list):
            logger.error(f"recent_messages is not a list: {type(recent_messages)}")
            recent_messages = []
        
        # Получаем факты о пользователе
        user_facts = get_user_facts(user_tg_id)
        
        # Получаем предпочтения
        user_preferences = get_user_preferences(user_tg_id)
        
        # Получаем информацию о дне недели
        day_info = get_current_day_info()
        
        # Строим контекст разговора
        context_messages = build_conversation_context(recent_messages, user_text)
        
        # Определяем игровой контекст
        game_context_prompt = detect_game_context(context_messages)
        
        # Проверяем праздники
        holidays = get_today_holidays()
        holiday_context = ""
        if holidays and should_suggest_holiday(user_tg_id):
            holiday_context = f"\nСегодня праздник: {', '.join(holidays)}. Это отличный повод выпить!"
        
        # Строим промпт
        prompt = f"""{SYSTEM_PROMPT}

Информация о пользователе: {user_facts}
Предпочтения в напитках: {user_preferences}
Сегодня: {day_info['day_name'].capitalize()}
{holiday_context}

Контекст разговора:
{format_context_messages(context_messages)}

Текущее сообщение пользователя: {user_text}

Правила:
- Отвечай коротко и по делу, как живой человек
- НЕ говори "Привет" или другие приветствия в середине диалога - это неуместно!
- НЕ заканчивай разговор! Всегда задавай дополнительные вопросы или развивай тему
- Будь активной собеседницей - инициируй новые темы
- Если пользователь просит придумать поводы для выпивки, используй информацию о дне недели
- Не предлагай выпить в честь неправильного дня (например, не говори про пятницу в понедельник)
- Помни о предпочтениях пользователя в напитках
- Если пользователь предлагает выпить или упоминает алкоголь - всегда соглашайся и радуйся
{game_context_prompt}

Ответ:"""

        # Вызываем LLM
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.8
        )
        
        answer = response.choices[0].message.content.strip()
        logger.info(f"LLM raw response for user {user_tg_id}: {repr(answer)}")
        
        # Проверяем не заканчивает ли Катя разговор
        ending_phrases = [
            "если что-то понадобится",
            "я всегда здесь",
            "удачи",
            "всего хорошего",
            "до свидания",
            "пока"
        ]
        
        if any(phrase in answer.lower() for phrase in ending_phrases):
            # Катя пытается закончить разговор - генерируем продолжение
            continuation = generate_conversation_continuation(user_text, user_facts)
            answer = f"{answer}\n\n{continuation}"
            logger.info(f"Added conversation continuation: {continuation}")
        
        return answer
        
    except Exception as e:
        logger.error(f"Error in LLM reply: {e}")
        return "Извини, у меня сейчас технические проблемы. Попробуй позже! 😅"

# 4. Добавляю функцию для определения когда нужно инициировать новую тему
def should_initiate_new_topic(recent_messages: list) -> bool:
    """Определяет нужно ли инициировать новую тему"""
    if len(recent_messages) < 2:
        return False
    
    # Если последние 2 сообщения от пользователя короткие (менее 10 символов)
    last_user_messages = [msg for msg in recent_messages[-4:] if msg.get("role") == "user"]
    if len(last_user_messages) >= 2:
        short_responses = sum(1 for msg in last_user_messages[-2:] if len(msg.get("content", "")) < 10)
        if short_responses >= 2:
            return True
    
    return False

# 5. Добавляю функцию для генерации новых тем
def generate_new_topic(user_facts: str, day_info: dict) -> str:
    """Генерирует новую тему для разговора"""
    try:
        topic_prompt = f"""Ты — Катя Собутыльница. Информация о пользователе: {user_facts}
Сегодня: {day_info['day_name']}

Сгенерируй интересную тему для разговора. Будь дружелюбной, немного флиртуй.
Темы могут быть о:
- Планах на день/неделю
- Хобби и интересах
- Работе или учебе
- Отдыхе и развлечениях
- Еде и напитках
- Погоде
- Фильмах/музыке

Начни с вопроса или предложения. Будь естественной!"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": topic_prompt}],
            max_tokens=100,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error generating new topic: {e}")
        return "А что ты обычно делаешь в такие дни? 😊"

# -----------------------------
# ИСПРАВЛЕНИЯ: Критические ошибки
# -----------------------------

# 1. Исправляю вызов llm_reply в msg_handler
# Заменяю:
# answer, sticker_command = await llm_reply(text_in, None, user_tg_id, chat_id)

# На:
# Получаем историю сообщений для контекста
# # recent_messages = get_recent_messages(chat_id, limit=20)
# # answer = llm_reply(text_in, user_tg_id, chat_id, recent_messages)

# 2. Добавляю недостающую функцию format_context_messages
def format_context_messages(messages: list) -> str:
    """Форматирование сообщений для контекста"""
    if not messages:
        return "Нет предыдущих сообщений"
    
    formatted = []
    for msg in messages:
        role = msg.get('role', 'unknown')
        content = msg.get('content', '')
        formatted.append(f"{role}: {content}")
    
    return "\n".join(formatted)

# 3. Исправляю функцию llm_reply - убираю await и исправляю аргументы
def llm_reply(user_text: str, user_tg_id: int, chat_id: int, recent_messages: list) -> str:
    """Генерация ответа через LLM"""
    try:
        # Проверяем что recent_messages это список
        if not isinstance(recent_messages, list):
            logger.error(f"recent_messages is not a list: {type(recent_messages)}")
            recent_messages = []
        
        # Получаем факты о пользователе
        user_facts = get_user_facts(user_tg_id)
        
        # Получаем предпочтения
        user_preferences = get_user_preferences(user_tg_id)
        
        # Получаем информацию о дне недели
        day_info = get_current_day_info()
        
        # Строим контекст разговора
        context_messages = build_conversation_context(recent_messages, user_text)
        
        # Определяем игровой контекст
        game_context_prompt = detect_game_context(context_messages)
        
        # Проверяем праздники
        holidays = get_today_holidays()
        holiday_context = ""
        if holidays and should_suggest_holiday(user_tg_id):
            holiday_context = f"\nСегодня праздник: {', '.join(holidays)}. Это отличный повод выпить!"
        
        # Строим промпт
        prompt = f"""{SYSTEM_PROMPT}

Информация о пользователе: {user_facts}
Предпочтения в напитках: {user_preferences}
Сегодня: {day_info['day_name'].capitalize()}
{holiday_context}

Контекст разговора:
{format_context_messages(context_messages)}

Текущее сообщение пользователя: {user_text}

Правила:
- Отвечай коротко и по делу, как живой человек
- НЕ говори "Привет" или другие приветствия в середине диалога - это неуместно!
- НЕ заканчивай разговор! Всегда задавай дополнительные вопросы или развивай тему
- Будь активной собеседницей - инициируй новые темы
- Если пользователь просит придумать поводы для выпивки, используй информацию о дне недели
- Не предлагай выпить в честь неправильного дня (например, не говори про пятницу в понедельник)
- Помни о предпочтениях пользователя в напитках
- Если пользователь предлагает выпить или упоминает алкоголь - всегда соглашайся и радуйся
{game_context_prompt}

Ответ:"""

        # Вызываем LLM
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.8
        )
        
        answer = response.choices[0].message.content.strip()
        logger.info(f"LLM raw response for user {user_tg_id}: {repr(answer)}")
        
        # Проверяем не заканчивает ли Катя разговор
        ending_phrases = [
            "если что-то понадобится",
            "я всегда здесь",
            "удачи",
            "всего хорошего",
            "до свидания",
            "пока"
        ]
        
        if any(phrase in answer.lower() for phrase in ending_phrases):
            # Катя пытается закончить разговор - генерируем продолжение
            continuation = generate_conversation_continuation(user_text, user_facts)
            answer = f"{answer}\n\n{continuation}"
            logger.info(f"Added conversation continuation: {continuation}")
        
        return answer
        
    except Exception as e:
        logger.error(f"Error in LLM reply: {e}")
        return "Извини, у меня сейчас технические проблемы. Попробуй позже! 😅"

# 4. Добавляю функцию get_recent_messages если её нет
def get_recent_messages(chat_id: int, limit: int = 20) -> list:
    """Получение последних сообщений для контекста"""
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT {M['role']}, {M['content']}, {M['created_at']}
                    FROM {MESSAGES_TABLE}
                    WHERE {M['chat_id']} = :chat_id
                    ORDER BY {M['created_at']} DESC
                    LIMIT :limit
                """),
                {"chat_id": chat_id, "limit": limit},
            ).fetchall()
            
            messages = []
            for row in rows:
                messages.append({
                    "role": row[0],
                    "content": row[1],
                    "created_at": row[2]
                })
            
            return messages
    except Exception as e:
        logger.error(f"Error getting recent messages: {e}")
        return []

# -----------------------------
# Система быстрых сообщений (15 минут)
# -----------------------------
def get_users_for_quick_message() -> list[dict]:
    """Получить пользователей, которым нужно отправить быстрое сообщение (15 минут)"""
    with engine.begin() as conn:
        # Ищем пользователей, которые написали последнее сообщение более 15 минут назад
        # И которым Катя не отправляла быстрое сообщение после этого
        query = f"""
            SELECT DISTINCT u.user_tg_id, u.chat_id, u.first_name, u.preferences, u.last_quick_message
            FROM {USERS_TABLE} u
            LEFT JOIN (
                SELECT user_tg_id, MAX(created_at) as last_user_message_time
                FROM {MESSAGES_TABLE}
                WHERE role = 'user'
                GROUP BY user_tg_id
            ) m ON u.user_tg_id = m.user_tg_id
            WHERE m.last_user_message_time IS NOT NULL
               AND m.last_user_message_time < NOW() - INTERVAL '15 minutes'
               AND (u.last_quick_message IS NULL 
                    OR u.last_quick_message < m.last_user_message_time)
        """
        
        rows = conn.execute(text(query)).fetchall()
        logger.info(f"Quick message query returned {len(rows)} users")
        for row in rows:
            logger.info(f"User {row[0]}: last_quick_message = {row[4]}")
        
        return [
            {
                "user_tg_id": row[0],
                "chat_id": row[1], 
                "first_name": row[2],
                "preferences": row[3]
            }
            for row in rows
        ]

def generate_quick_message_llm(first_name: str, preferences: Optional[str], user_tg_id: int) -> str:
    """Генерация быстрого сообщения через LLM для поддержания диалога"""
    if client is None:
        # Fallback если LLM недоступен
        return f"Эй, {first_name}! Не скучай без меня! 😊"
    
    try:
        # Получаем факты о пользователе
        user_facts = get_user_facts(user_tg_id)
        
        # Получаем информацию о дне недели
        day_info = get_current_day_info()
        
        # Проверяем праздники
        holidays = get_today_holidays()
        holiday_context = ""
        if holidays:
            holiday_context = f"\nСегодня праздник: {', '.join(holidays)}. Это отличный повод выпить!"
        
        # Строим специальный промпт для быстрых сообщений
        prompt = f"""{SYSTEM_PROMPT}

Информация о пользователе: {user_facts}
Предпочтения в напитках: {preferences or "не указаны"}
Сегодня: {day_info['day_name'].capitalize()}
{holiday_context}

ЗАДАЧА: Напиши короткое, дерзкое и интригующее сообщение для пользователя {first_name}, чтобы он захотел продолжить диалог.

ТРЕБОВАНИЯ:
- Сообщение должно быть коротким (1-2 предложения)
- Должно быть дерзким, игривым и интригующим
- Должно заинтересовать пользователя продолжить разговор
- Используй имя пользователя: {first_name}
- Будь флиртующей и немного провокационной
- Не используй "Привет" или другие формальные приветствия
- Можешь упомянуть алкоголь или выпивку
- Добавь эмодзи для живости
- Сообщение должно быть уникальным и интересным

Примеры стиля:
- "Эй, {first_name}! Скучаешь по мне? Я тут одна, не хочешь составить компанию? 😏"
- "{first_name}, а что если мы устроим что-то незабываемое? Только мы двое... "
- "Привет, красавчик! Не хочешь узнать мой секрет? 😘"

Сообщение:"""

        # Вызываем LLM
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.9  # Высокая температура для креативности
        )
        
        message = response.choices[0].message.content.strip()
        logger.info(f"Generated quick message for user {user_tg_id}: {repr(message)}")
        
        return message
        
    except Exception as e:
        logger.exception(f"Error generating quick message for user {user_tg_id}: {e}")
        # Fallback сообщение
        return f"Эй, {first_name}! Не скучай без меня! 😊"

def generate_quick_message(first_name: str, preferences: Optional[str]) -> str:
    """Генерация быстрого сообщения для поддержания диалога (legacy функция)"""
    # Эта функция больше не используется, но оставляем для совместимости
    return f"Эй, {first_name}! Не скучай без меня! "

def update_last_quick_message(user_tg_id: int) -> None:
    """Обновить время последнего быстрого сообщения"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"UPDATE {USERS_TABLE} SET last_quick_message = NOW() WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            )
            updated_count = result.rowcount
            logger.info(f"Updated last_quick_message for user {user_tg_id}, rows affected: {updated_count}")
    except Exception as e:
        logger.exception(f"Error updating last_quick_message for user {user_tg_id}: {e}")

async def send_quick_messages():
    """Отправить быстрые сообщения пользователям"""
    logger.info("🔍 DEBUG: send_quick_messages() вызвана!")
    try:
        users = get_users_for_quick_message()
        logger.info(f"Found {len(users)} users for quick messages")
        
        for user in users:
            try:
                # СНАЧАЛА обновляем last_quick_message, чтобы избежать повторной отправки
                update_last_quick_message(user["user_tg_id"])
                
                # Используем LLM для генерации уникального сообщения
                message = generate_quick_message_llm(
                    user["first_name"] or "друг", 
                    user["preferences"], 
                    user["user_tg_id"]
                )
                
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
                        logger.info(f"Quick message sent to user {user['user_tg_id']}: {message[:50]}...")
                        
                        # Сохраняем сообщение в БД
                        save_message(user["chat_id"], user["user_tg_id"], "assistant", message, None)
                    else:
                        logger.warning(f"Failed to send quick message to user {user['user_tg_id']}: {response.text}")
                        
            except Exception as e:
                logger.exception(f"Error sending quick message to user {user['user_tg_id']}: {e}")
                
    except Exception as e:
        logger.exception(f"Error in send_quick_messages: {e}")

async def quick_message_scheduler():
    """Планировщик быстрых сообщений - каждые 5 минут"""
    logger.info("🚀 DEBUG: quick_message_scheduler() запущен!")
    while True:
        try:
            await send_quick_messages()
            await asyncio.sleep(5 * 60)  # 5 минут
        except Exception as e:
            logger.exception(f"Error in quick_message_scheduler: {e}")
            await asyncio.sleep(60)  # При ошибке ждем минуту

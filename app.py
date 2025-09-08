import os
import httpx
import re
import logging
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
                chat_id BIGINT NOT NULL,
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
                message_id INTEGER,
                sticker_sent TEXT
            )
        """))
        
        # Добавляем недостающие колонки к существующим таблицам
        try:
            # Добавляем колонку chat_id к users если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {USERS_TABLE} 
                ADD COLUMN IF NOT EXISTS chat_id BIGINT NOT NULL DEFAULT 0
            """))
            
            # Добавляем колонку user_tg_id к users если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {USERS_TABLE} 
                ADD COLUMN IF NOT EXISTS user_tg_id BIGINT UNIQUE
            """))
            
            # Добавляем колонку age к users если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {USERS_TABLE} 
                ADD COLUMN IF NOT EXISTS age INTEGER
            """))
            
            # Добавляем колонку user_tg_id к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS user_tg_id BIGINT NOT NULL DEFAULT 0
            """))
            
            # Добавляем колонку message_id к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS message_id INTEGER
            """))
            
            # Добавляем колонку reply_to_message_id к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS reply_to_message_id INTEGER
            """))
            
            # Добавляем колонку sticker_sent к messages если её нет
            conn.execute(DDL(f"""
                ALTER TABLE {MESSAGES_TABLE} 
                ADD COLUMN IF NOT EXISTS sticker_sent TEXT
            """))
            
        except Exception as e:
            logger.warning(f"Some columns might already exist: {e}")
        
        # Создание таблицы для бесплатных напитков Кати
        conn.execute(DDL(f"""
            CREATE TABLE IF NOT EXISTS katya_free_drinks (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                drinks_used INTEGER DEFAULT 0,
                date_reset DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
    
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
# Telegram Application
# -----------------------------
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is empty (webhook/бот работать не будет)")

tapp: Optional[Application] = None

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
    
    # Запускаем ping-бот в фоне
    asyncio.create_task(ping_scheduler())
    
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
                    logger.info(f"Parsed age from text '{text}': {age}")
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
            logger.info(f"Loaded context: {content[:100]}...")
            return content
    except Exception as e:
        logger.exception("Failed to load context from Context.txt")
        return "Ты — Катя Собутыльница: живая девушка, дружелюбная, немного флиртуешь, любишь пошутить и выпить. Ты любишь алкогольные напитки и всегда готова поддержать компанию за рюмочку. Отвечай коротко и по делу, как живой человек. Если пользователь предлагает выпить или упоминает алкоголь - ты всегда соглашаешься и радуешься."

SYSTEM_PROMPT = load_context()

def get_user_name(user_tg_id: int) -> Optional[str]:
    """Получение имени пользователя из БД"""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {U['first_name']} FROM {USERS_TABLE} WHERE {U['user_tg_id']} = :tg_id"),
            {"tg_id": user_tg_id},
        ).fetchone()
        return row[0] if row and row[0] else None

async def llm_reply(user_text: str, username: Optional[str], user_tg_id: int, chat_id: int) -> tuple[str, Optional[str]]:
    """
    Генерирует ответ LLM и возвращает (ответ, команда_стикера_или_None)
    """
    if client is None:
        return FALLBACK_OPENAI_UNAVAILABLE, None
    
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
        
        # Получаем имя пользователя из БД для более точного обращения
        user_name = get_user_name(user_tg_id)
        if user_name:
            messages.append({"role": "system", "content": f"Имя пользователя: {user_name}. Обращайся к нему по имени, а не по username."})
        
        # Добавляем историю сообщений (в обратном порядке для правильной последовательности)
        for msg in reversed(recent_messages[-3:]):  # только последние 3 сообщения
            messages.append(msg)
        
        messages.append({"role": "user", "content": user_text})
        
        logger.info(f"LLM request for user {user_tg_id}: {len(messages)} messages, age: {user_age}, name: {user_name}")

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
        
        # Проверяем упоминание алкоголя в ответе LLM
        lower_response = response_text.lower()
        if any(word in lower_response for word in ["вино", "винца", "винцо", "🍷"]):
            sticker_command = "[SEND_DRINK_WINE]"
        elif any(word in lower_response for word in ["водка", "водочка", "🍸"]):
            sticker_command = "[SEND_DRINK_VODKA]"
        elif any(word in lower_response for word in ["виски", "вискарь", "🥃"]):
            sticker_command = "[SEND_DRINK_WHISKY]"
        elif any(word in lower_response for word in ["пиво", "пивка", "🍺"]):
            sticker_command = "[SEND_DRINK_BEER]"
        elif any(word in lower_response for word in ["радость", "радуешься", "весело", "😊"]):
            sticker_command = "[SEND_KATYA_HAPPY]"
        elif any(word in lower_response for word in ["грустно", "тоска", "😢"]):
            sticker_command = "[SEND_KATYA_SAD]"
        
        if sticker_command:
            logger.info(f"Auto-detected sticker: {sticker_command} for user {user_tg_id}")
        else:
            logger.info(f"No sticker auto-detected for user {user_tg_id}")
        
        return response_text, sticker_command
        
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return FALLBACK_OPENAI_UNAVAILABLE, None

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
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик сообщений"""
    if not update.message or not update.message.text:
        return

    text_in = update.message.text
    chat_id = update.message.chat_id
    user_tg_id = update.message.from_user.id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    message_id = update.message.message_id

    logger.info("Received message: %s from user %s", text_in, user_tg_id)

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

    # 4) Генерируем ответ через OpenAI
    answer, sticker_command = await llm_reply(text_in, username, user_tg_id, chat_id)

    # 5) Отправляем ответ
    try:
        sent_message = await update.message.reply_text(answer)
    except Exception:
        logger.exception("Failed to send reply")
        return

    # 6) Отправляем стикер если нужно
    if sticker_command:
        await send_sticker_by_command(chat_id, sticker_command)
            
        # Увеличиваем счетчик бесплатных напитков Кати
        increment_katya_drinks(chat_id)
            
        # Проверяем лимит бесплатных напитков Кати
        if not can_katya_drink_free(chat_id):
            # Катя исчерпала лимит бесплатных напитков
            await send_gift_request(chat_id, user_tg_id)
        
        # Сохраняем ответ бота с информацией о стикере
        save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id, None, sticker_command)
    else:
        # Сохраняем ответ бота без стикера
        save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id)

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

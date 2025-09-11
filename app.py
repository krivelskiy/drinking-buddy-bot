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
import json

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

# Импорты новых модулей
from database import (
    save_user, save_message, get_recent_messages, get_user_name, get_user_age,
    update_user_age, update_user_preferences, reset_quick_message_flag,
    update_last_quick_message, get_users_for_quick_message, get_users_for_auto_message,
    update_last_auto_message
)
from llm_utils import llm_reply, generate_quick_message_llm, generate_auto_message_llm
from schedulers import quick_message_scheduler, auto_message_scheduler, ping_scheduler
from message_handlers import handle_user_message, handle_successful_payment
from gender_llm import generate_gender_appropriate_gratitude
from db_utils import get_user_gender, update_user_gender, update_user_name_and_gender
from migrations import run_migrations
from katya_utils import send_gift_request
from katya_utils import send_gift_request

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

def get_fallback_response(function_name: str) -> str:
    """Получить fallback ответ для функции"""
    fallbacks = {
        "msg_handler": "Извини, у меня сейчас проблемы с ответом. Попробуй позже! ��",
        "start": "Привет! Я Катя, но у меня сейчас проблемы. Попробуй позже! 😅",
        "help": "Извини, справка временно недоступна. Попробуй позже! ��",
        "stats": "Статистика временно недоступна. Попробуй позже! 😅",
        "gift": "Подарки временно недоступны. Попробуй позже! ��",
    }
    return fallbacks.get(function_name, "Извини, что-то пошло не так. Попробуй позже! 😅")

async def notify_critical_error(function_name: str, error: str):
    """Уведомление о критической ошибке"""
    try:
        # Здесь можно добавить отправку уведомлений в Telegram или другие системы мониторинга
        logger.critical(f"CRITICAL ERROR in {function_name}: {error}")
    except Exception:
        pass

# -----------------------------
# Инициализация
# -----------------------------

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)

# Инициализируем клиент OpenAI
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Создаем приложение FastAPI
app = FastAPI(title="Drinking Buddy Bot", version="1.0.0")

# Создаем приложение Telegram
telegram_app = Application.builder().token(BOT_TOKEN).build()
bot = telegram_app.bot

# Словари для полей таблиц
U = DB_FIELDS['users']
M = DB_FIELDS['messages']

# -----------------------------
# Инициализация базы данных
# -----------------------------

def init_db():
    """Инициализация базы данных"""
    try:
        with engine.begin() as conn:
            # Создаем таблицу пользователей
            conn.execute(DDL(f"""
                CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
                    {U['user_tg_id']} BIGINT PRIMARY KEY,
                    {U['chat_id']} BIGINT NOT NULL,
                    {U['username']} VARCHAR(255),
                    {U['first_name']} VARCHAR(255),
                    {U['last_name']} VARCHAR(255),
                    {U['age']} INTEGER,
                    {U['preferences']} TEXT,
                    {U['last_quick_message']} TIMESTAMPTZ,
                    {U['last_auto_message']} TIMESTAMPTZ,
                    {U['last_stats_reminder']} TIMESTAMPTZ,
                    {U['last_preference_ask']} TIMESTAMPTZ,
                    {U['last_holiday_mention']} TIMESTAMPTZ,
                    {U['last_drink_warning']} TIMESTAMPTZ,
                    {U['last_activity']} TIMESTAMPTZ DEFAULT NOW(),
                    {U['created_at']} TIMESTAMPTZ DEFAULT NOW(),
                    {U['updated_at']} TIMESTAMPTZ DEFAULT NOW(),
                    {U['tg_id']} BIGINT UNIQUE,
                    {U['quick_message_sent']} BOOLEAN DEFAULT TRUE,
                    {U['gender']} VARCHAR(10)
                )
            """))
            
            # Создаем таблицу сообщений
            conn.execute(DDL(f"""
                CREATE TABLE IF NOT EXISTS {MESSAGES_TABLE} (
                    {M['id']} SERIAL PRIMARY KEY,
                    {M['chat_id']} BIGINT NOT NULL,
                    {M['user_tg_id']} BIGINT NOT NULL,
                    {M['role']} VARCHAR(20) NOT NULL,
                    {M['content']} TEXT NOT NULL,
                    {M['message_id']} BIGINT,
                    {M['reply_to_message_id']} BIGINT,
                    {M['sticker_sent']} VARCHAR(50),
                    {M['created_at']} TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            
            # Создаем таблицу бесплатных напитков Кати
            conn.execute(DDL(f"""
                CREATE TABLE IF NOT EXISTS katya_free_drinks (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    drinks_count INTEGER DEFAULT 0,
                    last_reset TIMESTAMPTZ DEFAULT NOW(),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            
            # Создаем таблицу записей о выпитом пользователями
            conn.execute(DDL(f"""
                CREATE TABLE IF NOT EXISTS user_drinks (
                    id SERIAL PRIMARY KEY,
                    user_tg_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    drink_type VARCHAR(50) NOT NULL,
                    amount INTEGER NOT NULL,
                    unit VARCHAR(20) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            
            # Добавляем поле quick_message_sent если его нет
            conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS quick_message_sent BOOLEAN DEFAULT TRUE"))
            
            logger.info("Database initialized successfully")
            
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

# -----------------------------
# FastAPI startup/shutdown events
# -----------------------------

@app.on_event("startup")
async def startup_event():
    """Инициализация при запуске"""
    try:
        # Инициализируем базу данных
        init_db()
        
        # Запускаем миграции
        run_migrations()
        
        # Инициализируем Telegram приложение
        await telegram_app.initialize()
        
        # Добавляем обработчики
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("stats", stats_command))
        telegram_app.add_handler(CommandHandler("gift", gift_command))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
        telegram_app.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
        telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
        
        # Запускаем планировщики
        asyncio.create_task(ping_scheduler())
        asyncio.create_task(quick_message_scheduler(bot))
        asyncio.create_task(auto_message_scheduler(bot))
        
        logger.info("Application started successfully")
        
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Очистка при завершении"""
    try:
        await telegram_app.shutdown()
        logger.info("Application shutdown completed")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

# -----------------------------
# Обработчики команд
# -----------------------------

@safe_execute
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    if not update.message:
        return
    
    # Сохраняем пользователя в БД
    save_user(update, context)
    
    # Получаем имя пользователя
    user_name = get_user_name(update.message.from_user.id) or "друг"
    
    # Генерируем приветствие с учетом пола
    greeting = generate_gender_appropriate_greeting(user_name, get_user_gender(update.message.from_user.id))
    
    await update.message.reply_text(greeting)
    
    # Сохраняем сообщение бота
    save_message(
        update.message.chat_id, 
        update.message.from_user.id, 
        "assistant", 
        greeting, 
        None, 
        None, 
        None
    )

@safe_execute
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help"""
    if not update.message:
        return
    
    help_text = """
 **Привет! Я Катя Собутыльница!**

**Что я умею:**
• Общаться и флиртовать 😉
• Предлагать напитки и выпивать вместе
• Вести статистику твоего выпитого 📊
• Напоминать о праздниках
• Играть в игры 🎮

**Команды:**
/start - Начать общение
/help - Показать эту справку
/stats - Показать статистику выпитого
/gift - Подарить напиток Кате

**Как пользоваться:**
Просто пиши мне сообщения, и я отвечу! Можешь рассказать о себе, что пьешь, сколько лет и т.д.

**Статистика:**
Напиши "статистика" чтобы увидеть сколько ты выпил сегодня и за неделю!

**Подарки:**
Используй /gift чтобы подарить мне напиток!

Давай знакомиться! 😘
    """
    
    await update.message.reply_text(help_text)

@safe_execute
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /stats"""
    if not update.message:
        return
    
    user_tg_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    # Генерируем статистику
    stats = generate_drinks_stats(user_tg_id)
    
    await update.message.reply_text(f"📊 **Твоя статистика выпитого:**\n\n{stats}")
    
    # Сохраняем сообщение бота
    save_message(chat_id, user_tg_id, "assistant", f"📊 **Твоя статистика выпитого:**\n\n{stats}", None, None, None)

@safe_execute
async def gift_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /gift"""
    if not update.message:
        return
    
    user_tg_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    # Отправляем запрос на подарок
    await send_gift_request(context.bot, chat_id, user_tg_id)

# -----------------------------
# Основной обработчик сообщений
# -----------------------------

@safe_execute
async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик сообщений"""
    if not update.message or not update.message.text:
        return
    
    # Используем новый модуль для обработки сообщений
    await handle_user_message(update, context)

# -----------------------------
# Обработчики платежей
# -----------------------------

@safe_execute
async def pre_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик pre-checkout запроса"""
    query = update.pre_checkout_query
    await query.answer(ok=True)

@safe_execute
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка успешной оплаты"""
    # Используем новый модуль для обработки платежей
    await handle_successful_payment(update, context)

# -----------------------------
# Функции для работы с напитками
# -----------------------------

def can_katya_drink_free(chat_id: int) -> bool:
    """Проверить, может ли Катя пить бесплатно"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("SELECT drinks_count FROM katya_free_drinks WHERE chat_id = :chat_id"),
                {"chat_id": chat_id}
            ).fetchone()
            
            if result:
                return result[0] < 5  # Максимум 5 бесплатных напитков
            else:
                # Создаем новую запись
                conn.execute(
                    text("INSERT INTO katya_free_drinks (chat_id, drinks_count) VALUES (:chat_id, 0)"),
                    {"chat_id": chat_id}
                )
                return True
    except Exception as e:
        logger.error(f"Error checking free drinks: {e}")
        return False

def increment_katya_drinks(chat_id: int) -> None:
    """Увеличить счетчик напитков Кати"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE katya_free_drinks SET drinks_count = drinks_count + 1 WHERE chat_id = :chat_id"),
                {"chat_id": chat_id}
            )
    except Exception as e:
        logger.error(f"Error incrementing drinks: {e}")

# -----------------------------
# Функции для работы со стикерами
# -----------------------------

async def send_sticker_by_command(chat_id: int, command: str) -> None:
    """Отправить стикер по команде"""
    try:
        if command in STICKERS:
            sticker_id = STICKERS[command]
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            logger.info(f"Sent sticker {command} to chat {chat_id}")
        else:
            logger.warning(f"Unknown sticker command: {command}")
    except Exception as e:
        logger.error(f"Error sending sticker {command}: {e}")

# -----------------------------
# Функции для работы с подарками
# -----------------------------

# -----------------------------
# Функции для работы со статистикой
# -----------------------------

def generate_drinks_stats(user_tg_id: int) -> str:
    """Генерировать статистику выпитого"""
    try:
        with engine.begin() as conn:
            # Статистика за сегодня
            today_stats = conn.execute(
                text("""
                    SELECT drink_type, SUM(amount) as total_amount, unit
                    FROM user_drinks
                    WHERE user_tg_id = :user_tg_id
                    AND DATE(created_at) = CURRENT_DATE
                    GROUP BY drink_type, unit
                    ORDER BY total_amount DESC
                """),
                {"user_tg_id": user_tg_id}
            ).fetchall()
            
            # Статистика за неделю
            week_stats = conn.execute(
                text("""
                    SELECT drink_type, SUM(amount) as total_amount, unit
                    FROM user_drinks
                    WHERE user_tg_id = :user_tg_id
                    AND created_at >= CURRENT_DATE - INTERVAL '7 days'
                    GROUP BY drink_type, unit
                    ORDER BY total_amount DESC
                """),
                {"user_tg_id": user_tg_id}
            ).fetchall()
            
            # Формируем текст статистики
            stats_text = "**Сегодня:**\n"
            if today_stats:
                for stat in today_stats:
                    stats_text += f"• {stat[0]}: {stat[1]} {stat[2]}\n"
            else:
                stats_text += "• Пока ничего не выпито\n"
            
            stats_text += "\n**За неделю:**\n"
            if week_stats:
                for stat in week_stats:
                    stats_text += f"• {stat[0]}: {stat[1]} {stat[2]}\n"
            else:
                stats_text += "• Пока ничего не выпито\n"
            
            return stats_text
            
    except Exception as e:
        logger.error(f"Error generating stats: {e}")
        return "Ошибка при получении статистики. Попробуй позже! 😅"

def save_drink_record(user_tg_id: int, chat_id: int, drink_info: dict) -> None:
    """Сохранить запись о выпитом"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO user_drinks (user_tg_id, chat_id, drink_type, amount, unit)
                    VALUES (:user_tg_id, :chat_id, :drink_type, :amount, :unit)
                """),
                {
                    "user_tg_id": user_tg_id,
                    "chat_id": chat_id,
                    "drink_type": drink_info["drink_type"],
                    "amount": drink_info["amount"],
                    "unit": drink_info["unit"]
                }
            )
    except Exception as e:
        logger.error(f"Error saving drink record: {e}")

def should_remind_about_stats(user_tg_id: int) -> bool:
    """Проверить, нужно ли напомнить о статистике"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"SELECT last_stats_reminder FROM {USERS_TABLE} WHERE user_tg_id = :user_tg_id"),
                {"user_tg_id": user_tg_id}
            ).fetchone()
            
            if result and result[0]:
                # Проверяем, прошло ли 24 часа
                return (datetime.now() - result[0]).total_seconds() > 86400
            else:
                return True
    except Exception as e:
        logger.error(f"Error checking stats reminder: {e}")
        return False

def update_stats_reminder(user_tg_id: int) -> None:
    """Обновить время последнего напоминания о статистике"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {USERS_TABLE} SET last_stats_reminder = NOW() WHERE user_tg_id = :user_tg_id"),
                {"user_tg_id": user_tg_id}
            )
    except Exception as e:
        logger.error(f"Error updating stats reminder: {e}")

# -----------------------------
# Функции для работы с полом
# -----------------------------

def generate_gender_appropriate_greeting(name: str, gender: Optional[str] = None) -> str:
    """Генерировать приветствие с учетом пола"""
    if gender == "male":
        return f"Привет, {name}! Рада познакомиться! 😘"
    elif gender == "female":
        return f"Привет, {name}! Рада познакомиться!"
    else:
        return f"Привет, {name}! Рада познакомиться! 😘"

# -----------------------------
# FastAPI endpoints
# -----------------------------

@app.get("/")
async def root():
    """Корневой endpoint"""
    return {
        "message": "Drinking Buddy Bot API",
        "status": "running",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "metrics": "/metrics",
            "webhook": "/webhook/{bot_token} (POST)"
        },
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    """Проверка здоровья приложения"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/metrics")
async def get_metrics():
    """Получение метрик приложения"""
    return {
        "error_counts": dict(error_counts),
        "last_error_times": {k: datetime.fromtimestamp(v).isoformat() for k, v in last_error_time.items()},
        "timestamp": datetime.now().isoformat()
    }

@app.post("/webhook/{bot_token}")
async def webhook(bot_token: str, request: Request):
    """Webhook для Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# -----------------------------
# Запуск приложения (только для локального тестирования)
# -----------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
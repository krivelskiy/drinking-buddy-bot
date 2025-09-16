"""
Утилиты для работы с Катей (напитки, стикеры, подарки)
"""
import logging
import random
import json
from sqlalchemy import create_engine, text
from config import DATABASE_URL
from constants import STICKERS

logger = logging.getLogger(__name__)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)

def can_katya_drink_free(chat_id: int) -> bool:
    """Проверить, может ли Катя пить бесплатно"""
    try:
        with engine.begin() as conn:
            # Проверяем запись с учетом даты сброса
            result = conn.execute(
                text("""
                    SELECT drinks_count, date_reset 
                    FROM katya_free_drinks 
                    WHERE chat_id = :chat_id
                """),
                {"chat_id": chat_id}
            ).fetchone()
            
            if result:
                drinks_count, date_reset = result
                
                # ✅ Проверяем, нужно ли сбросить счетчик (прошло больше суток)
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                
                if date_reset:
                    # Приводим date_reset к timezone-aware если нужно
                    if hasattr(date_reset, 'tzinfo') and date_reset.tzinfo is None:
                        date_reset = date_reset.replace(tzinfo=timezone.utc)
                    elif not hasattr(date_reset, 'tzinfo'):
                        # Если это date, конвертируем в datetime
                        from datetime import date
                        if isinstance(date_reset, date):
                            date_reset = datetime.combine(date_reset, datetime.min.time()).replace(tzinfo=timezone.utc)
                    
                    # Если прошло больше суток, сбрасываем счетчик
                    if (now - date_reset).days >= 1:
                        conn.execute(
                            text("""
                                UPDATE katya_free_drinks 
                                SET drinks_count = 0, date_reset = NOW() 
                                WHERE chat_id = :chat_id
                            """),
                            {"chat_id": chat_id}
                        )
                        logger.info(f"Reset drinks count for chat {chat_id} - new day started")
                        return True  # После сброса можно пить
                
                return drinks_count < 5  # Максимум 5 бесплатных напитков
            else:
                # Создаем новую запись
                conn.execute(
                    text("""
                        INSERT INTO katya_free_drinks (chat_id, drinks_count, date_reset) 
                        VALUES (:chat_id, 0, NOW())
                    """),
                    {"chat_id": chat_id}
                )
                return True  # Первый напиток бесплатный
                
    except Exception as e:
        logger.error(f"Error checking free drinks: {e}")
        return True  # По умолчанию разрешаем пить

def increment_katya_drinks(chat_id: int) -> None:
    """Увеличить счетчик напитков Кати"""
    try:
        with engine.begin() as conn:
            # Проверяем, нужно ли сбросить счетчик перед увеличением
            result = conn.execute(
                text("""
                    SELECT drinks_count, date_reset 
                    FROM katya_free_drinks 
                    WHERE chat_id = :chat_id
                """),
                {"chat_id": chat_id}
            ).fetchone()
            
            if result:
                drinks_count, date_reset = result
                
                # Проверяем, нужно ли сбросить счетчик (прошло больше суток)
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                
                if date_reset:
                    # Приводим date_reset к timezone-aware если нужно
                    if hasattr(date_reset, 'tzinfo') and date_reset.tzinfo is None:
                        date_reset = date_reset.replace(tzinfo=timezone.utc)
                    elif not hasattr(date_reset, 'tzinfo'):
                        # Если это date, конвертируем в datetime
                        from datetime import date
                        if isinstance(date_reset, date):
                            date_reset = datetime.combine(date_reset, datetime.min.time()).replace(tzinfo=timezone.utc)
                    
                    # Если прошло больше суток, сбрасываем счетчик
                    if (now - date_reset).days >= 1:
                        conn.execute(
                            text("""
                                UPDATE katya_free_drinks 
                                SET drinks_count = 1, date_reset = NOW() 
                                WHERE chat_id = :chat_id
                            """),
                            {"chat_id": chat_id}
                        )
                        logger.info(f"Reset and incremented drinks count for chat {chat_id} - new day started")
                        return
                
                # Обычное увеличение счетчика
                conn.execute(
                    text("UPDATE katya_free_drinks SET drinks_count = drinks_count + 1 WHERE chat_id = :chat_id"),
                    {"chat_id": chat_id}
                )
            else:
                # Создаем новую запись
                conn.execute(
                    text("""
                        INSERT INTO katya_free_drinks (chat_id, drinks_count, date_reset) 
                        VALUES (:chat_id, 1, NOW())
                    """),
                    {"chat_id": chat_id}
                )
    except Exception as e:
        logger.error(f"Error incrementing drinks: {e}")

async def update_katya_free_drinks(chat_id: int, increment: int) -> None:
    """Обновить счетчик бесплатных напитков Кати"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE katya_free_drinks SET drinks_count = drinks_count + :increment WHERE chat_id = :chat_id"),
                {"increment": increment, "chat_id": chat_id}
            )
    except Exception as e:
        logger.error(f"Error updating free drinks: {e}")

async def send_sticker_by_command(bot, chat_id: int, command: str) -> None:
    """Отправить стикер по команде"""
    try:
        # Маппинг команд на стикеры
        command_mapping = {
            "[SEND_DRINK_VODKA]": STICKERS["DRINK_VODKA"],
            "[SEND_DRINK_WHISKY]": STICKERS["DRINK_WHISKY"],
            "[SEND_DRINK_WINE]": STICKERS["DRINK_WINE"],
            "[SEND_DRINK_BEER]": STICKERS["DRINK_BEER"],
            "[SEND_KATYA_HAPPY]": STICKERS["KATYA_HAPPY"],
            "[SEND_KATYA_SAD]": STICKERS["KATYA_SAD"],
            "[SEND_SAD_STICKER]": STICKERS["KATYA_SAD"],  # Добавляем маппинг для грустного стикера
            "[SEND_HAPPY_STICKER]": STICKERS["KATYA_HAPPY"],  # Добавляем маппинг для веселого стикера
        }
        
        if command in command_mapping:
            sticker_id = command_mapping[command]
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            logger.info(f"Sent sticker {command} to chat {chat_id}")
        elif command in STICKERS:
            sticker_id = STICKERS[command]
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            logger.info(f"Sent sticker {command} to chat {chat_id}")
        else:
            logger.warning(f"Unknown sticker command: {command}")
    except Exception as e:
        logger.error(f"Error sending sticker {command}: {e}")

async def send_gift_request(bot, chat_id: int, user_tg_id: int) -> None:
    """Отправить запрос на подарок с inline кнопками для выбора напитка"""
    try:
        from database import get_user_name
        from db_utils import get_user_gender
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        user_name = get_user_name(user_tg_id) or "друг"
        user_gender = get_user_gender(user_tg_id) or "неизвестен"
        
        # Список доступных напитков
        drinks = [
            {"name": "Вино", "emoji": "🍷", "price": 250},
            {"name": "Водка", "emoji": "🍸", "price": 100},
            {"name": "Виски", "emoji": "🥃", "price": 500},
            {"name": "Пиво", "emoji": "🍺", "price": 50},
        ]
        
        # Создаем inline кнопки для каждого напитка
        keyboard = []
        for drink in drinks:
            button = InlineKeyboardButton(
                f"{drink['name']} {drink['emoji']} - {drink['price']} ⭐",
                callback_data=f"gift_{drink['name'].lower()}"
            )
            keyboard.append([button])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Более тонкие и естественные описания
        descriptions = [
            "Катя мечтает о вкусном напитке... Может, угостишь её? 💕",
            "Кате так хочется выпить! Подаришь ей радость? 💕",
            "Катя смотрит на бар с надеждой... Поможешь? 💕",
            "Кате нужен напиток для хорошего настроения! 💕",
            "Катя просит угостить... Будет очень благодарна! 😘"
        ]
        
        description = random.choice(descriptions)
        
        logger.info(f"Sending gift request with inline buttons for {len(drinks)} drinks")
        
        # Отправляем сообщение с inline кнопками
        await bot.send_message(
            chat_id=chat_id,
            text=f"{description}\n\nВыбери напиток для Кати:",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error sending gift request: {e}") 
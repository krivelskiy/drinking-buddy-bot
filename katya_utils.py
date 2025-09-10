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
            # Сначала проверяем, существует ли таблица и поле
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
                return True  # Первый напиток бесплатный
                
    except Exception as e:
        logger.error(f"Error checking free drinks: {e}")
        # Если ошибка с полем, пытаемся пересоздать таблицу
        try:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS katya_free_drinks"))
                conn.execute(text("CREATE TABLE katya_free_drinks (chat_id INTEGER PRIMARY KEY, drinks_count INTEGER)"))
                return True # После пересоздания таблицы первый напиток бесплатный
        except Exception as re_e:
            logger.error(f"Error recreating table: {re_e}")
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
        if command in STICKERS:
            sticker_id = STICKERS[command]
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            logger.info(f"Sent sticker {command} to chat {chat_id}")
        else:
            logger.warning(f"Unknown sticker command: {command}")
    except Exception as e:
        logger.error(f"Error sending sticker {command}: {e}")

async def send_gift_request(bot, chat_id: int, user_tg_id: int) -> None:
    """Отправить запрос на подарок"""
    try:
        # Получаем информацию о пользователе для персонализации
        from database import get_user_name
        from db_utils import get_user_gender
        
        user_name = get_user_name(user_tg_id) or "друг"
        user_gender = get_user_gender(user_tg_id) or "неизвестен"
        
        # Список доступных напитков с ценами (временно все по 1 звезде)
        drinks = [
            {"name": "Пиво", "emoji": "🍺", "price": 1},
            {"name": "Водка", "emoji": "🍸", "price": 1},
            {"name": "Вино", "emoji": "🍷", "price": 1},
            {"name": "Виски", "emoji": "🥃", "price": 1},
            {"name": "Шампанское", "emoji": "🍾", "price": 1},
        ]
        
        # Выбираем случайный напиток
        drink = random.choice(drinks)
        
        # Создаем payload для платежа
        payload = json.dumps({
            "drink_name": drink["name"],
            "drink_emoji": drink["emoji"]
        })
        
        # Более тонкие и естественные описания
        descriptions = [
            f"Катя мечтает о {drink['name'].lower()}... Может, угостишь её? 💕",
            f"Кате так хочется {drink['name'].lower()}! Подаришь ей радость? 💕",
            f"Катя смотрит на {drink['name'].lower()} с надеждой... Поможешь? 💕",
            f"Кате нужен {drink['name'].lower()} для хорошего настроения! 💕",
            f"Катя просит {drink['name'].lower()}... Будет очень благодарна! 😘"
        ]
        
        description = random.choice(descriptions)
        
        # Отправляем invoice
        await bot.send_invoice(
            chat_id=chat_id,
            title=f"Угости Катю: {drink['name']} {drink['emoji']}",
            description=description,
            payload=payload,
            provider_token="",  # Для тестовых платежей
            currency="XTR",  # Telegram Stars
            prices=[{"label": f"{drink['name']} {drink['emoji']}", "amount": drink["price"]}]
        )
        
    except Exception as e:
        logger.error(f"Error sending gift request: {e}") 
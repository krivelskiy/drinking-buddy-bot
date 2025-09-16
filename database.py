"""
Модуль для работы с базой данных
"""
import logging
from sqlalchemy import create_engine, text, DDL
from typing import Optional, List, Dict, Any
from config import DATABASE_URL
from constants import USERS_TABLE, MESSAGES_TABLE, DB_FIELDS

logger = logging.getLogger(__name__)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)

# Словари для полей таблиц
U = DB_FIELDS['users']
M = DB_FIELDS['messages']

def save_user(update, context):
    """Сохранение пользователя в БД"""
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

def get_recent_messages(chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Получить последние сообщения для контекста"""
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT role, content, created_at
                    FROM {MESSAGES_TABLE}
                    WHERE chat_id = :chat_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"chat_id": chat_id, "limit": limit}
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

def get_user_name(user_tg_id: int) -> Optional[str]:
    """Получить имя пользователя"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"SELECT first_name FROM {USERS_TABLE} WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            ).fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting user name: {e}")
        return None

def get_user_age(user_tg_id: int) -> Optional[int]:
    """Получить возраст пользователя"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"SELECT age FROM {USERS_TABLE} WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            ).fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting user age: {e}")
        return None

def update_user_age(user_tg_id: int, age: int) -> None:
    """Обновить возраст пользователя"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {USERS_TABLE} SET age = :age WHERE user_tg_id = :tg_id"),
                {"age": age, "tg_id": user_tg_id}
            )
            logger.info(f"Updated age for user {user_tg_id} to {age}")
    except Exception as e:
        logger.error(f"Error updating user age: {e}")

def update_user_preferences(user_tg_id: int, preferences: str) -> None:
    """Обновить предпочтения пользователя"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {USERS_TABLE} SET preferences = :preferences WHERE user_tg_id = :tg_id"),
                {"preferences": preferences, "tg_id": user_tg_id}
            )
            logger.info(f"Updated preferences for user {user_tg_id} to {preferences}")
    except Exception as e:
        logger.error(f"Error updating user preferences: {e}")

def reset_quick_message_flag(user_tg_id: int) -> None:
    """Сбросить флаг быстрого сообщения при получении сообщения от пользователя"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"UPDATE {USERS_TABLE} SET quick_message_sent = FALSE WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            )
            updated_count = result.rowcount
            logger.info(f"Reset quick_message_sent flag for user {user_tg_id}, rows affected: {updated_count}")
    except Exception as e:
        logger.error(f"Error resetting quick_message_sent flag for user {user_tg_id}: {e}")

def update_last_quick_message(user_tg_id: int) -> None:
    """Обновить время последнего быстрого сообщения и установить флаг"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"UPDATE {USERS_TABLE} SET last_quick_message = NOW(), quick_message_sent = TRUE WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            )
            updated_count = result.rowcount
            logger.info(f"Updated last_quick_message and set quick_message_sent=TRUE for user {user_tg_id}, rows affected: {updated_count}")
    except Exception as e:
        logger.error(f"Error updating last_quick_message for user {user_tg_id}: {e}")

def get_users_for_quick_message() -> List[Dict[str, Any]]:
    """Получить пользователей, которым нужно отправить быстрое сообщение (15 минут)"""
    with engine.begin() as conn:
        # Новый алгоритм: ищем пользователей, которые написали последнее сообщение более 15 минут назад
        # И у которых флаг quick_message_sent = FALSE
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
               AND u.quick_message_sent = FALSE
               AND (u.last_auto_message IS NULL OR u.last_auto_message < NOW() - INTERVAL '1 hour')
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

def get_users_for_auto_message() -> List[Dict[str, Any]]:
    """Получить пользователей, которым нужно отправить автоматическое сообщение (24 часа)"""
    with engine.begin() as conn:
        # Новый алгоритм: ищем пользователей, которые написали последнее сообщение более 24 часов назад
        # И которым не отправляли auto message в последние 24 часа
        query = f"""
            SELECT DISTINCT u.user_tg_id, u.chat_id, u.first_name, u.preferences
            FROM {USERS_TABLE} u
            LEFT JOIN (
                SELECT user_tg_id, MAX(created_at) as last_user_message_time
                FROM {MESSAGES_TABLE}
                WHERE role = 'user'
                GROUP BY user_tg_id
            ) m ON u.user_tg_id = m.user_tg_id
            WHERE m.last_user_message_time IS NOT NULL
              AND m.last_user_message_time < NOW() - INTERVAL '24 hours'
              AND (u.last_auto_message IS NULL OR u.last_auto_message < NOW() - INTERVAL '24 hours')
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

def update_last_auto_message(user_tg_id: int) -> None:
    """Обновить время последнего автоматического сообщения"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"UPDATE {USERS_TABLE} SET last_auto_message = NOW() WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            )
            updated_count = result.rowcount
            logger.info(f"Updated last_auto_message for user {user_tg_id}, rows affected: {updated_count}")
    except Exception as e:
        logger.error(f"Error updating last_auto_message for user {user_tg_id}: {e}")

def get_user_preferences(user_tg_id: int) -> Optional[str]:
    """Получить предпочтения пользователя"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"SELECT preferences FROM {USERS_TABLE} WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            )
            row = result.fetchone()
            if row and row[0]:
                return row[0]
            return None
    except Exception as e:
        logger.error(f"Error getting user preferences: {e}")
        return None 
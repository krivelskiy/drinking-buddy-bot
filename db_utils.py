"""
Утилиты для работы с базой данных
"""
import logging
from sqlalchemy import create_engine, text
from typing import Optional
from config import DATABASE_URL
from constants import USERS_TABLE

logger = logging.getLogger(__name__)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)

def get_user_gender(user_tg_id: int) -> Optional[str]:
    """Получить пол пользователя из базы данных"""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(f"SELECT gender FROM {USERS_TABLE} WHERE user_tg_id = :tg_id"),
                {"tg_id": user_tg_id}
            ).fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting user gender: {e}")
        return None

def update_user_gender(user_tg_id: int, gender: str) -> None:
    """Обновить пол пользователя в базе данных"""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {USERS_TABLE} SET gender = :gender WHERE user_tg_id = :tg_id"),
                {"gender": gender, "tg_id": user_tg_id}
            )
            logger.info(f"Updated gender for user {user_tg_id} to {gender}")
    except Exception as e:
        logger.error(f"Error updating user gender: {e}")

def get_user_name(user_tg_id: int) -> Optional[str]:
    """Получить имя пользователя из базы данных"""
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

def update_user_name_and_gender(user_tg_id: int, first_name: str) -> None:
    """Обновить имя пользователя и автоматически определить пол через LLM"""
    try:
        from gender_llm import detect_gender_with_llm
        
        # Определяем пол по имени через LLM
        gender = detect_gender_with_llm(first_name)
        
        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {USERS_TABLE} SET first_name = :name, gender = :gender WHERE user_tg_id = :tg_id"),
                {"name": first_name, "gender": gender, "tg_id": user_tg_id}
            )
            logger.info(f"Updated name for user {user_tg_id} to {first_name} and gender to {gender}")
    except Exception as e:
        logger.error(f"Error updating user name and gender: {e}") 
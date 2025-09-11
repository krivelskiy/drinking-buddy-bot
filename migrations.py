"""
Миграции базы данных
"""
import logging
from sqlalchemy import create_engine, text, DDL
from config import DATABASE_URL
from constants import USERS_TABLE

logger = logging.getLogger(__name__)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)

def add_gender_field():
    """Добавить поле gender в таблицу users"""
    try:
        with engine.begin() as conn:
            conn.execute(DDL(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS gender VARCHAR(10)"))
            logger.info("✅ Added gender field to users table")
    except Exception as e:
        logger.error(f"Error adding gender field: {e}")

def add_drinks_count_field():
    """Добавить поле drinks_count в таблицу katya_free_drinks"""
    try:
        with engine.begin() as conn:
            conn.execute(DDL("ALTER TABLE katya_free_drinks ADD COLUMN IF NOT EXISTS drinks_count INTEGER DEFAULT 0"))
            logger.info("✅ Added drinks_count field to katya_free_drinks table")
    except Exception as e:
        logger.error(f"Error adding drinks_count field: {e}")

def run_migrations():
    """Запустить все миграции"""
    logger.info("🔄 Running database migrations...")
    
    # Добавляем поле gender
    add_gender_field()
    
    # Добавляем поле drinks_count
    add_drinks_count_field()
    
    logger.info("✅ All migrations completed")

if __name__ == "__main__":
    run_migrations() 
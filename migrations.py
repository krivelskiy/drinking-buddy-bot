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

def run_migrations():
    """Запустить все миграции"""
    logger.info("🔄 Running database migrations...")
    
    # Добавляем поле gender
    add_gender_field()
    
    logger.info("✅ All migrations completed")

if __name__ == "__main__":
    run_migrations() 
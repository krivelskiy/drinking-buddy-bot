"""
Утилиты для работы со статистикой
"""
import logging
from datetime import datetime
from sqlalchemy import create_engine, text
from config import DATABASE_URL
from constants import USERS_TABLE

logger = logging.getLogger(__name__)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)

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
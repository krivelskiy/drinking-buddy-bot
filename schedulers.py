"""
Модуль для планировщиков
"""
import logging
import asyncio
import httpx
from typing import List, Dict, Any
from database import (
    get_users_for_quick_message, 
    get_users_for_auto_message,
    update_last_quick_message,
    update_last_auto_message
)
from llm_utils import generate_quick_message_llm, generate_auto_message_llm
from config import RENDER_EXTERNAL_URL

logger = logging.getLogger(__name__)

async def send_quick_messages(bot):
    """Отправить быстрые сообщения пользователям"""
    logger.info("🔍 DEBUG: send_quick_messages() вызвана!")
    try:
        users = get_users_for_quick_message()
        logger.info(f"Found {len(users)} users for quick messages")
        
        for user in users:
            try:
                # Обновляем время последнего быстрого сообщения
                update_last_quick_message(user["user_tg_id"])
                
                # Генерируем сообщение через LLM
                message = generate_quick_message_llm(
                    user["first_name"], 
                    user["preferences"], 
                    user["user_tg_id"]
                )
                
                # Отправляем сообщение
                await bot.send_message(chat_id=user["chat_id"], text=message)
                
                logger.info(f"Quick message sent to user {user['user_tg_id']}: {message[:50]}...")
                
            except Exception as e:
                logger.error(f"Error sending quick message to user {user['user_tg_id']}: {e}")
                
    except Exception as e:
        logger.error(f"Error in send_quick_messages: {e}")

async def send_auto_messages(bot):
    """Отправить автоматические сообщения пользователям"""
    logger.info(" DEBUG: send_auto_messages() вызвана!")
    try:
        users = get_users_for_auto_message()
        logger.info(f"Found {len(users)} users for auto messages")
        
        for user in users:
            try:
                # Обновляем время последнего автоматического сообщения
                update_last_auto_message(user["user_tg_id"])
                
                # Генерируем сообщение через LLM
                message = generate_auto_message_llm(
                    user["first_name"], 
                    user["preferences"], 
                    user["user_tg_id"]
                )
                
                # Отправляем сообщение
                await bot.send_message(chat_id=user["chat_id"], text=message)
                
                logger.info(f"Auto message sent to user {user['user_tg_id']}: {message[:50]}...")
                
            except Exception as e:
                logger.error(f"Error sending auto message to user {user['user_tg_id']}: {e}")
                
    except Exception as e:
        logger.error(f"Error in send_auto_messages: {e}")

async def quick_message_scheduler(bot):
    """Планировщик быстрых сообщений (каждые 30 секунд)"""
    logger.info("🚀 DEBUG: quick_message_scheduler() запущен!")
    while True:
        try:
            await send_quick_messages(bot)
        except Exception as e:
            logger.error(f"Error in quick_message_scheduler: {e}")
        
        await asyncio.sleep(30)  # Проверяем каждые 30 секунд

async def auto_message_scheduler(bot):
    """Планировщик автоматических сообщений (каждые 5 минут)"""
    logger.info("🚀 DEBUG: auto_message_scheduler() запущен!")
    while True:
        try:
            await send_auto_messages(bot)
        except Exception as e:
            logger.error(f"Error in auto_message_scheduler: {e}")
        
        await asyncio.sleep(86400)  # Проверяем каждые 24 часа

async def ping_scheduler():
    """Планировщик пингов для поддержания активности Render"""
    logger.info("🚀 DEBUG: ping_scheduler() запущен!")
    while True:
        try:
            # Делаем реальный HTTP-запрос к нашему приложению
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{RENDER_EXTERNAL_URL}/ping", timeout=10.0)
                if response.status_code == 200:
                    logger.info("🏓 Ping successful - Render kept alive!")
                else:
                    logger.warning(f"🏓 Ping failed with status {response.status_code}, response: {response.text[:100]}")
        except Exception as e:
            logger.error(f"Error in ping_scheduler: {e}")
        
        await asyncio.sleep(3600)  # Пинг каждые 60 минут (3600 секунд) 
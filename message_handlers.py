"""
Модуль для обработчиков сообщений
"""
import logging
import asyncio
import re
from telegram import Update
from telegram.ext import ContextTypes
from typing import Optional

from database import (
    save_message, 
    reset_quick_message_flag, 
    get_user_name, 
    get_user_age,
    update_user_age,
    update_user_preferences
)
from llm_utils import llm_reply
from gender_llm import generate_gender_appropriate_gratitude
from db_utils import update_user_name_and_gender, get_user_gender
from stats_utils import generate_drinks_stats, save_drink_record, should_remind_about_stats, update_stats_reminder
from katya_utils import can_katya_drink_free, send_sticker_by_command, increment_katya_drinks, send_gift_request

logger = logging.getLogger(__name__)

def parse_age_from_text(text: str) -> Optional[int]:
    """Парсинг возраста из текста"""
    # Ищем числа от 10 до 100
    age_pattern = r'\b(1[0-9]|[2-9][0-9]|100)\b'
    matches = re.findall(age_pattern, text)
    
    if matches:
        # Берем первое найденное число
        age = int(matches[0])
        if 10 <= age <= 100:
            return age
    
    return None

def parse_drink_preferences(text: str) -> Optional[str]:
    """Парсинг предпочтений в напитках из текста"""
    drink_keywords = ['пиво', 'водка', 'вино', 'виски', 'коньяк', 'шампанское', 'ром', 'джин', 'текила']
    
    found_preferences = []
    text_lower = text.lower()
    
    for drink in drink_keywords:
        if drink in text_lower:
            found_preferences.append(drink)
    
    if found_preferences:
        return ', '.join(found_preferences)
    
    return None

def parse_drink_info(text: str) -> Optional[dict]:
    """Парсинг информации о выпитом из текста"""
    # Паттерны для поиска информации о выпитом
    patterns = [
        r'выпил\s+(\d+)\s*(?:г|грамм|мл|литр|л|стакан|стакана|стаканов|банка|банки|банок|бутылка|бутылки|бутылок|рюмка|рюмки|рюмок)',
        r'выпила\s+(\d+)\s*(?:г|грамм|мл|литр|л|стакан|стакана|стаканов|банка|банки|банок|бутылка|бутылки|бутылок|рюмка|рюмки|рюмок)',
        r'(\d+)\s*(?:г|грамм|мл|литр|л|стакан|стакана|стаканов|банка|банки|банок|бутылка|бутылки|бутылок|рюмка|рюмки|рюмок)',
    ]
    
    text_lower = text.lower()
    
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            amount = int(match.group(1))
            
            # Определяем тип напитка
            drink_type = "алкоголь"
            if any(word in text_lower for word in ['пиво', 'beer']):
                drink_type = "пиво"
            elif any(word in text_lower for word in ['водка', 'vodka']):
                drink_type = "водка"
            elif any(word in text_lower for word in ['вино', 'wine']):
                drink_type = "вино"
            elif any(word in text_lower for word in ['виски', 'whisky']):
                drink_type = "виски"
            
            # Определяем единицу измерения
            unit = "порций"
            if any(word in text_lower for word in ['г', 'грамм']):
                unit = "г"
            elif any(word in text_lower for word in ['мл']):
                unit = "мл"
            elif any(word in text_lower for word in ['литр', 'л']):
                unit = "л"
            elif any(word in text_lower for word in ['стакан', 'стакана', 'стаканов']):
                unit = "стаканов"
            elif any(word in text_lower for word in ['банка', 'банки', 'банок']):
                unit = "банок"
            elif any(word in text_lower for word in ['бутылка', 'бутылки', 'бутылок']):
                unit = "бутылок"
            elif any(word in text_lower for word in ['рюмка', 'рюмки', 'рюмок']):
                unit = "рюмок"
            
            return {
                "drink_type": drink_type,
                "amount": amount,
                "unit": unit
            }
    
    return None

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка сообщения от пользователя"""
    if not update.message or not update.message.text:
        return
    
    text_in = update.message.text
    user_tg_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    logger.info(f"Received message: {text_in} from user {user_tg_id}")
    
    try:
        # Сохраняем сообщение пользователя в базу данных
        save_message(chat_id, user_tg_id, "user", text_in)
        
        # Обновляем имя пользователя и определяем пол если оно изменилось
        if update.message.from_user.first_name:
            current_name = get_user_name(user_tg_id)
            current_gender = get_user_gender(user_tg_id)
            
            # Определяем пол если имя изменилось ИЛИ пол не определен
            if current_name != update.message.from_user.first_name or not current_gender:
                update_user_name_and_gender(user_tg_id, update.message.from_user.first_name)
        
        # Сбрасываем флаг быстрого сообщения при получении сообщения от пользователя
        reset_quick_message_flag(user_tg_id)
        
        # ВАЖНО: Проверяем статистику ПЕРВОЙ!
        if any(word in text_in.lower() for word in ['статистика', 'сколько выпил', 'сколько пил', 'статистик']):
            stats = generate_drinks_stats(user_tg_id)
            await update.message.reply_text(f"📊 **Твоя статистика выпитого:**\n\n{stats}")
            save_message(chat_id, user_tg_id, "assistant", f"📊 **Твоя статистика выпитого:**\n\n{stats}", None, None, None)
            return  # ВАЖНО: return чтобы НЕ вызывать LLM
        
        # Остальные проверки...
        # 1) Проверяем на упоминание возраста
        age = parse_age_from_text(text_in)
        if age:
            try:
                update_user_age(user_tg_id, age)
                logger.info("Updated user age to %d", age)
            except Exception:
                logger.exception("Failed to update age")
        
        # 2) Проверяем на упоминание предпочтений в напитках
        preferences = parse_drink_preferences(text_in)
        if preferences:
            try:
                update_user_preferences(user_tg_id, preferences)
                logger.info("Updated user preferences to %s", preferences)
            except Exception:
                logger.exception("Failed to update preferences")
        
        # 3) Проверяем на упоминание выпитого
        drink_info = parse_drink_info(text_in)
        if drink_info:
            try:
                save_drink_record(user_tg_id, chat_id, drink_info)
                logger.info("✅ Saved drink record: %s", drink_info)
            except Exception:
                logger.exception("Failed to save drink record")
        
        # 4) Проверяем, нужно ли напомнить о статистике
        if should_remind_about_stats(user_tg_id):
            reminder_msg = "💡 Кстати, я могу вести статистику твоего выпитого! Просто напиши 'статистика' и я покажу сколько ты выпил сегодня и за неделю! 📊\n\nА чтобы я не забывала - каждый раз когда пьешь, просто напиши мне что и сколько! Например: \"выпил 2 пива\" или \"выпил 100г водки\" 🍷"
            await update.message.reply_text(reminder_msg)
            save_message(chat_id, user_tg_id, "assistant", reminder_msg, None, None, None)
            update_stats_reminder(user_tg_id)
            return
        
        # 5) Генерируем ответ через OpenAI
        from database import get_recent_messages
        recent_messages = get_recent_messages(chat_id, limit=12)
        answer = llm_reply(text_in, user_tg_id, chat_id, recent_messages)
        
        # Определяем команду стикера на основе ответа LLM
        sticker_command = None
        if any(keyword in answer.lower() for keyword in ["выпьем", "выпьемте", "пьем", "пьемте", "выпьем вместе", "давай выпьем", "пей", "выпей", "наливай"]):
            sticker_command = "[SEND_DRINK_BEER]"
        elif any(keyword in answer.lower() for keyword in ["водка", "водочка", "водочки"]):
            sticker_command = "[SEND_DRINK_VODKA]"
        elif any(keyword in answer.lower() for keyword in ["вино", "винцо", "винца"]):
            sticker_command = "[SEND_DRINK_WINE]"
        elif any(keyword in answer.lower() for keyword in ["виски", "вискарь", "вискаря"]):
            sticker_command = "[SEND_DRINK_WHISKEY]"
        elif any(keyword in answer.lower() for keyword in ["грустно", "печально", "тоскливо", "грустная"]):
            sticker_command = "[SEND_SAD_STICKER]"
        elif any(keyword in answer.lower() for keyword in ["радостно", "весело", "счастливо", "радостная"]):
            sticker_command = "[SEND_HAPPY_STICKER]"

        # 6) Отправляем ответ
        try:
            sent_message = await update.message.reply_text(answer)
            
            # 7) Проверяем, можем ли отправить стикер (если LLM его определил)
            if sticker_command:
                # Проверяем, может ли Катя пить бесплатно
                if can_katya_drink_free(chat_id):
                    # Отправляем стикер и увеличиваем счетчик
                    await send_sticker_by_command(context.bot, chat_id, sticker_command)
                    increment_katya_drinks(chat_id)
                    
                    # Сохраняем ответ бота С информацией о стикере
                    save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id, None, sticker_command)
                else:
                    # Катя исчерпала лимит бесплатных напитков - НЕ отправляем стикер
                    await send_gift_request(context.bot, chat_id, user_tg_id)
                    
                    # Сохраняем ответ бота БЕЗ стикера
                    save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id)
            else:
                # Сохраняем ответ бота без стикера
                save_message(chat_id, user_tg_id, "assistant", answer, sent_message.message_id)
        except Exception as e:
            logger.exception(f"Message handler error: {e}")
    except Exception as e:
        logger.error(f"Error in handle_user_message: {e}")
        # Катя всегда должна отвечать, даже при ошибках
        fallback_message = "Извини, у меня что-то сломалось... Но я все равно готова выпить с тобой! 🍻"
        await update.message.reply_text(fallback_message)
        save_message(chat_id, user_tg_id, "assistant", fallback_message, None, None, None)

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка успешной оплаты"""
    try:
        # Для SUCCESSFUL_PAYMENT сообщений данные находятся в update.message.successful_payment
        if not update.message or not update.message.successful_payment:
            logger.error("No successful payment data found in update")
            return
            
        payment = update.message.successful_payment
        user_tg_id = update.message.from_user.id
        chat_id = update.message.chat_id
        
        # Получаем информацию о напитке из payload с проверкой
        drink_name = 'напиток'
        drink_emoji = ''
        
        try:
            import json
            if payment.invoice_payload:
                # Проверяем, является ли payload JSON или простой строкой
                if payment.invoice_payload.startswith('{'):
                    payload_data = json.loads(payment.invoice_payload)
                    drink_name = payload_data.get('drink_name', 'напиток')
                    drink_emoji = payload_data.get('drink_emoji', '')
                else:
                    # Простая строка типа "gift_виски"
                    drink_name = "напиток"
                    drink_emoji = ""
                logger.info(f"Parsed payload: {payment.invoice_payload}")
            else:
                logger.warning("Empty invoice_payload")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse invoice_payload: {payment.invoice_payload}, error: {e}")
            # Используем значения по умолчанию
        
        # Генерируем благодарственные сообщения с учетом пола
        user_name = get_user_name(user_tg_id) or "друг"
        user_gender = get_user_gender(user_tg_id) or "neutral"
        
        gratitude_messages = generate_gender_appropriate_gratitude(user_name, user_gender, drink_name, drink_emoji)
        
        # Отправляем сообщения с задержкой
        for i, message in enumerate(gratitude_messages):
            await asyncio.sleep(1.5)  # Задержка между сообщениями
            await context.bot.send_message(chat_id=chat_id, text=message)
        
        # Отправляем стикер с выпиванием подарка
        from katya_utils import send_sticker_by_command, update_katya_free_drinks
        
        # Определяем правильную команду стикера на основе payload
        sticker_command = "[SEND_DRINK_BEER]"  # по умолчанию
        if "gift_вино" in payment.invoice_payload:
            sticker_command = "[SEND_DRINK_WINE]"
        elif "gift_водка" in payment.invoice_payload:
            sticker_command = "[SEND_DRINK_VODKA]"
        elif "gift_виски" in payment.invoice_payload:
            sticker_command = "[SEND_DRINK_WHISKY]"
        elif "gift_пиво" in payment.invoice_payload:
            sticker_command = "[SEND_DRINK_BEER]"
        
        await send_sticker_by_command(context.bot, chat_id, sticker_command)
        
        # Обновляем счетчик бесплатных напитков
        await update_katya_free_drinks(chat_id, 1)
        
        logger.info(f"Successful payment processed for user {user_tg_id}, drink: {drink_name}")
        
    except Exception as e:
        logger.error(f"Error processing successful payment: {e}") 
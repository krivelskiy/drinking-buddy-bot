"""
Модуль для работы с LLM
"""
import logging
from typing import Optional, List
from openai import OpenAI
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

# Инициализируем клиент OpenAI
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def load_system_prompt() -> str:
    """Загрузка системного промпта из Context.txt"""
    try:
        with open("Context.txt", "r", encoding="utf-8") as f:
            base_prompt = f.read().strip()
        # Добавляем требование говорить только на русском
        return base_prompt + "\n\nВАЖНО: Отвечай ТОЛЬКО на русском языке. Никогда не используй английские слова или фразы в ответах."
    except Exception as e:
        logger.error(f"Failed to load system prompt: {e}")
        return "Ты — Катя Собутыльница: живая девушка, дружелюбная, немного флиртуешь, любишь пошутить и выпить. Отвечай ТОЛЬКО на русском языке."

SYSTEM_PROMPT = load_system_prompt()

def llm_reply(text_in: str, user_tg_id: int, chat_id: int, recent_messages: List[dict]) -> str:
    """Генерация ответа через LLM"""
    if client is None:
        return "У меня сейчас проблемы с ответом. Попробуй позже! 😅"
    
    try:
        # Получаем информацию о пользователе
        from database import get_user_name, get_user_age, get_user_preferences
        from db_utils import get_user_gender
        
        user_name = get_user_name(user_tg_id) or "друг"
        user_age = get_user_age(user_tg_id)
        user_gender = get_user_gender(user_tg_id) or "неизвестен"
        user_preferences = get_user_preferences(user_tg_id)
        
        # Строим контекст из последних сообщений
        context_messages = []
        for msg in reversed(recent_messages[-6:]):  # Берем последние 6 сообщений
            context_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
        
        # Добавляем системный промпт
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # Добавляем информацию о пользователе (БЕЗ ВОЗРАСТА)
        user_info = f"Пользователь: {user_name}"
        if user_gender != "неизвестен":
            user_info += f", пол: {user_gender}"
        if user_preferences:
            user_info += f", любимый напиток: {user_preferences}"
        
        messages.append({"role": "system", "content": user_info})
        
        # Добавляем контекст сообщений
        messages.extend(context_messages)
        
        # Добавляем текущее сообщение пользователя
        messages.append({"role": "user", "content": text_in})
        
        # Отправляем запрос к LLM
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=200,
            temperature=0.8
        )
        
        response_text = resp.choices[0].message.content.strip()
        
        # Логируем полный ответ LLM для диагностики
        logger.info(f"LLM raw response for user {user_tg_id}: '{response_text}'")
        
        return response_text
        
    except Exception as e:
        logger.exception(f"LLM error for user {user_tg_id}: {e}")
        return "У меня сейчас проблемы с ответом. Попробуй позже! 😅"

def generate_quick_message_llm(first_name: str, preferences: Optional[str], user_tg_id: int) -> str:
    """Генерация быстрого сообщения через LLM для поддержания диалога"""
    if client is None:
        return f"Привет, {first_name}! Как дела? 😉"
    
    try:
        # Строим промпт для LLM (БЕЗ ВОЗРАСТА)
        prompt = f"""Ты — Катя Собутыльница. Напиши короткое сообщение пользователю {first_name} для поддержания диалога.

ТРЕБОВАНИЯ:
- Обратись по имени {first_name}
- Будь дружелюбной и немного флиртующей
- Намекни на выпивку или интересную беседу
- Будь дерзкой и заманчивой
- Максимум 2 предложения
- Добавь эмодзи
- НЕ используй приветствия типа "Привет" - это середина диалога
- НЕ упоминай возраст пользователя в тостах или сообщениях"""

        if preferences:
            prompt += f"\n- Учти предпочтения: {preferences}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.8
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"Error generating quick message: {e}")
        return f"Привет, {first_name}! Как дела? 😉"

def generate_auto_message_llm(first_name: str, preferences: Optional[str], user_tg_id: int) -> str:
    """Генерация автоматического сообщения через LLM"""
    if client is None:
        return f"Привет, {first_name}! Соскучился? 😉"
    
    try:
        # Строим промпт для LLM (БЕЗ ВОЗРАСТА)
        prompt = f"""Ты — Катя Собутыльница. Напиши заманчивое сообщение пользователю {first_name} чтобы вернуть его в диалог.

ТРЕБОВАНИЯ:
- Обратись по имени {first_name}
- Будь дружелюбной и флиртующей
- Намекни на выпивку или интересную беседу
- Будь дерзкой и заманчивой
- Максимум 2 предложения
- Добавь эмодзи
- НЕ используй приветствия типа "Привет" - это середина диалога
- НЕ упоминай возраст пользователя в тостах или сообщениях"""

        if preferences:
            prompt += f"\n- Учти предпочтения: {preferences}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.8
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"Error generating auto message: {e}")
        return f"Привет, {first_name}! Соскучился? 😉" 
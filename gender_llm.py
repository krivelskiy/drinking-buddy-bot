"""
Модуль для работы с полом пользователей через LLM
"""
import logging
from typing import Optional
from openai import OpenAI
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

# Инициализируем клиент OpenAI
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def detect_gender_with_llm(first_name: str) -> str:
    """Определяет пол пользователя по имени через LLM"""
    if not first_name or not client:
        return "neutral"
    
    try:
        prompt = f"""Определи пол человека по имени "{first_name}". 

Ответь только одним словом:
- "male" если это мужское имя
- "female" если это женское имя  
- "neutral" если не можешь определить

Имя: {first_name}"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1
        )
        
        gender = response.choices[0].message.content.strip().lower()
        
        # Проверяем что ответ корректный
        if gender in ["male", "female", "neutral"]:
            return gender
        else:
            return "neutral"
            
    except Exception as e:
        logger.error(f"Error detecting gender with LLM: {e}")
        return "neutral"

def generate_gender_appropriate_greeting(name: str, gender: str) -> str:
    """Генерирует приветствие с учетом пола через LLM"""
    if not client:
        return f"Привет, {name}! 👋"
    
    try:
        prompt = f"""Ты — Катя Собутыльница. Напиши приветствие пользователю {name} (пол: {gender}).

ТРЕБОВАНИЯ:
- Используй правильный род для пола {gender}
- Будь дружелюбной и немного флиртующей
- Максимум 2 предложения
- Добавь эмодзи
- НЕ используй приветствия типа "Привет" - это середина диалога

Примеры правильных обращений:
- Мужской род: "дорогой", "красавчик", "парень"
- Женский род: "дорогая", "красавица", "девушка"
- Нейтральный: "друг", "подруга"

Создай одно приветственное сообщение."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.8
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"Error generating greeting: {e}")
        return f"Привет, {name}! 👋"

def generate_gender_appropriate_gratitude(name: str, gender: str, drink_name: str, drink_emoji: str) -> list[str]:
    """Генерирует благодарственные сообщения с учетом пола через LLM"""
    # Проверяем и приводим параметры к правильным типам
    if not isinstance(name, str):
        name = str(name) if name else "друг"
    if not isinstance(gender, str):
        gender = str(gender) if gender else "neutral"
    if not isinstance(drink_name, str):
        drink_name = str(drink_name) if drink_name else "напиток"
    if not isinstance(drink_emoji, str):
        drink_emoji = str(drink_emoji) if drink_emoji else ""
    
    if not client:
        return [
            f"Ого! {name}, ты подарил(а) мне {drink_name}!",
            f"💕 Я так рада! Спасибо тебе огромное!",
            f"Ты самый(ая) лучший(ая)! Сейчас выпью твой подарок!",
            f"{drink_emoji} *выпивает* Ммм, как вкусно!",
            f"💖 Ты сделал(а) мой день! Обнимаю тебя! 🤗"
        ]
    
    try:
        prompt = f"""Ты — Катя Собутыльница. Пользователь {name} (пол: {gender}) подарил тебе {drink_name}.

ТРЕБОВАНИЯ:
- Используй правильный род для пола {gender}
- Будь очень радостной и благодарной
- Создай 5 коротких сообщений (по 1-2 предложения каждое)
- Добавь эмодзи
- Используй правильные окончания для мужского/женского рода
- Будь милой и флиртующей

Примеры правильных окончаний:
- Мужской род: "ты сделал", "ты самый лучший", "спасибо тебе"
- Женский род: "ты сделала", "ты самая лучшая", "спасибо тебе"

Создай 5 сообщений, разделенных переносами строк."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.8
        )
        
        messages = response.choices[0].message.content.strip().split('\n')
        return [msg.strip() for msg in messages if msg.strip()]
        
    except Exception as e:
        logger.error(f"Ошибка генерации благодарственных сообщений: {e}")
        # Fallback
        return [
            f"Ого! {name}, ты подарил(а) мне {drink_name}!",
            f"💕 Я так рада! Спасибо тебе огромное!",
            f"Ты самый(ая) лучший(ая)! Сейчас выпью твой подарок!",
            f"{drink_emoji} *выпивает* Ммм, как вкусно!",
            f"💖 Ты сделал(а) мой день! Обнимаю тебя! 🤗"
        ] 
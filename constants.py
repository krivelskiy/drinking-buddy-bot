from types import MappingProxyType

# Названия таблиц
USERS_TABLE = "users"
MESSAGES_TABLE = "messages"

# Единая «точка правды» для констант и названий полей
STICKERS = MappingProxyType({
    "KATYA_HAPPY":  "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
    "KATYA_SAD":    "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "DRINK_VODKA":  "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "DRINK_WHISKY": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "DRINK_WINE":   "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "DRINK_BEER":   "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
})

# Стикеры пива для триггеров
BEER_STICKERS = [
    "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE"
]

# Триггеры для отправки стикеров пива
STICKER_TRIGGERS = ["пей", "пей пиво", "выпей пива", "выпей", "наливай"]

# Маппинг ключевых слов → нужный стикер (регистронезависимо)
DRINK_KEYWORDS = MappingProxyType({
    "водка": "DRINK_VODKA",
    "vodka": "DRINK_VODKA",
    "виски": "DRINK_WHISKY",
    "whisky": "DRINK_WHISKY",
    "вино": "DRINK_WINE",
    "wine": "DRINK_WINE",
    "пиво": "DRINK_BEER",
    "beer": "DRINK_BEER",
})

# Названия столбцов БД — точные названия согласно схеме
DB_FIELDS = MappingProxyType({
    "users": {
        "id": "id",
        "user_tg_id": "user_tg_id",
        "chat_id": "chat_id",
        "username": "username",
        "first_name": "first_name",
        "last_name": "last_name",
        "age": "age",
        "preferences": "preferences",
        "last_quick_message": "last_quick_message",
        "last_auto_message": "last_auto_message",
        "last_stats_reminder": "last_stats_reminder",
        "last_preference_ask": "last_preference_ask",
        "last_holiday_mention": "last_holiday_mention",
        "last_drink_warning": "last_drink_warning",
        "last_activity": "last_activity",
        "created_at": "created_at",
        "updated_at": "updated_at",
        "tg_id": "tg_id",
        "quick_message_sent": "quick_message_sent",
        "gender": "gender"
    },
    "messages": {
        "id": "id",
        "chat_id": "chat_id",
        "user_tg_id": "user_tg_id",
        "role": "role",
        "content": "content",
        "created_at": "created_at",
        "reply_to_message_id": "reply_to_message_id",
        "message_id": "message_id",
        "sticker_sent": "sticker_sent"
    }
})

# Жёсткая заглушка, если OpenAI недоступен (диалог не продолжаем)
FALLBACK_OPENAI_UNAVAILABLE = "Сорри, у меня хрипит горло — давай поболтаем позже 🍷"

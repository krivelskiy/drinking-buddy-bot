from types import MappingProxyType

# Единая «точка правды» для констант и названий полей
STICKERS = MappingProxyType({
    "KATYA_HAPPY":  "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
    "KATYA_SAD":    "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "DRINK_VODKA":  "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "DRINK_WHISKY": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "DRINK_WINE":   "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "DRINK_BEER":   "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
})

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

# Названия столбцов users — чтобы не путать в коде
DB_FIELDS = MappingProxyType({
    "users": {
        "pk": "chat_id",
        "tg_id": "tg_id",
        "username": "username",
        "first_name": "first_name",
        "last_name": "last_name",
        "name": "name",
        "favorite_drinks": "favorite_drinks",
        "summary": "summary",
        "free_drinks": "free_drinks",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }
})

# Жёсткая заглушка, если OpenAI недоступен (диалог не продолжаем)
FALLBACK_OPENAI_UNAVAILABLE = "Сорри, у меня хрипит горло — давай поболтаем позже 🍷"

import os

def _to_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}

# === ОФИЦИАЛЬНЫЕ КЛЮЧИ ИЗ ОКРУЖЕНИЯ ===
BOT_TOKEN: str | None = os.getenv("BOT_TOKEN")
DATABASE_URL: str | None = os.getenv("DATABASE_URL")
APP_BASE_URL: str | None = os.getenv("APP_BASE_URL")  # публичный базовый URL сервиса
AUTO_SET_WEBHOOK: bool = _to_bool(os.getenv("AUTO_SET_WEBHOOK"), True)

# OpenAI может быть пустым — диалоги тогда будут без LLM
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")

# Для удобства: в деве можно положить .env, но на проде берём из Environment

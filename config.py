import os

# ВНИМАНИЕ: названия ключей согласованы и зафиксированы
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/drinking_buddy_db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # именно BOT_TOKEN, не TELEGRAM_TOKEN и не WEBHOOK_URL
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

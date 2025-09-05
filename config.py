import os

# Имя ключей согласовано: строго BOT_TOKEN (не TELEGRAM_TOKEN/TELEGRAM_BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # должен быть задан в Render → Environment
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")

# База для вебхука. На Render можно не задавать, тогда setWebhook будет пропущен.
# Если задаёте, то, например: https://drinking-buddy-bot.onrender.com
BASE_URL = os.getenv("BASE_URL", os.getenv("RENDER_EXTERNAL_URL", "")).strip()

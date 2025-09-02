import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update, Bot
from openai import OpenAI

logging.basicConfig(level=logging.INFO)

# Загружаем токены из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise ValueError("Не найдены BOT_TOKEN или OPENAI_API_KEY в переменных окружения")

bot = Bot(token=BOT_TOKEN)
app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY)


@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        return JSONResponse(status_code=403, content={"error": "Invalid token"})

    data = await request.json()
    update = Update.de_json(data, bot)

    if update.message and update.message.text:
        user_message = update.message.text

        try:
            # Отправляем сообщение в OpenAI
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Ты весёлый собутыльник. Отвечай дружелюбно, с юмором и иногда подсказывай тосты."},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=150
            )

            reply = response.choices[0].message.content.strip()
        except Exception as e:
            logging.error(f"OpenAI error: {e}")
            reply = "Ну, за это надо выпить! 🍻"

        await bot.send_message(chat_id=update.message.chat_id, text=reply)

    return JSONResponse(status_code=200, content={"ok": True})

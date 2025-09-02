import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from openai import OpenAI

# Храним токены в переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

client = OpenAI(api_key=OPENAI_API_KEY)

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/webhook/{token}")
async def webhook(request: Request, token: str):
    if token != TOKEN:
        return {"error": "invalid token"}
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}

async def drinking_buddy_reply(user_message: str) -> str:
    prompt = f"""
Ты виртуальный собутыльник. Отвечай дружески, с юмором,
как будто сидишь с пользователем за одним столом и выпиваешь.
Пользователь сказал: "{user_message}"
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=120
    )
    return response.choices[0].message.content.strip()

@dp.message(commands=["start"])
async def start_handler(message: types.Message):
    await message.answer("Эй! 🍻 Я твой виртуальный собутыльник. Пиши что угодно — поболтаем, расскажу историю или подскажу тост.")

@dp.message()
async def chat_handler(message: types.Message):
    reply = await drinking_buddy_reply(message.text)
    await message.answer(reply)

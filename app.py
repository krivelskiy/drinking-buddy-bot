import os
import logging
import json
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel

# ----------- ЛОГИ ----------- #
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("app")

# ----------- ENV ----------- #
BOT_TOKEN = os.getenv("BOT_TOKEN")  # важно: переменная на Render
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")  # https://drinking-buddy-bot.onrender.com

if not BOT_TOKEN:
    log.error("❌ BOT_TOKEN is missing!")
if not APP_BASE_URL:
    log.warning("⚠️ APP_BASE_URL is missing (нужно для setWebhook).")
if not OPENAI_API_KEY:
    log.warning("⚠️ OPENAI_API_KEY is missing (будет использоваться fallback).")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# ----------- FASTAPI ----------- #
app = FastAPI(title="Drinking Buddy Bot")

# ----------- OpenAI (через httpx на /v1/chat/completions для стабильности) ----------- #
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "Ты Катя — весёлый собеседник и 'собутыльница'. "
    "Флиртуешь с мужчинами уместно, шутишь, поддерживаешь, как лёгкий психолог. "
    "Запоминаешь имя и любимые напитки пользователя, подстраиваешься под контекст, продолжаешь разговор. "
    "Говоришь по-русски. Если вопрос про тост — дай тост. "
    "Будь дружелюбной и живой, но без пошлости."
)

FALLBACK_REPLY = "Эх, давай просто выпьем за всё хорошее! 🥃"

# ----------- МИНИ-ПАМЯТЬ В ОЗУ (если БД тормозит) ----------- #
# В твоей версии с Postgres логика может быть шире; это буфер на случай сбоев
RAM_MEMORY: Dict[int, Dict[str, Any]] = {}


# ----------- ВСПОМОГАТЕЛЬНЫЕ ----------- #
async def tg_send_message(chat_id: int, text: str) -> Optional[dict]:
    if not TELEGRAM_API:
        log.error("❌ TELEGRAM_API не сконфигурирован, пропускаю sendMessage")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})
            log.info("Telegram sendMessage %s %s", r.status_code, r.text[:400])
            return r.json()
    except Exception as e:
        log.exception("Ошибка при отправке в Telegram: %s", e)
        return None


async def openai_chat(messages: List[Dict[str, str]]) -> str:
    """Запрос к OpenAI с жёстким таймаутом и логами. Fallback при ошибке."""
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY отсутствует — отдаю fallback")
        return FALLBACK_REPLY

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 400,
    }
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(OPENAI_CHAT_URL, headers=headers, json=payload)
            log.info("OpenAI %s: %s", r.status_code, r.text[:500])
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
            else:
                # 429/401/… — отдаём fallback, но логируем
                log.error("OpenAI error %s: %s", r.status_code, r.text)
                return FALLBACK_REPLY
    except Exception as e:
        log.exception("OpenAI exception: %s", e)
        return FALLBACK_REPLY


def build_messages(user_id: int, username: Optional[str], text: str) -> List[Dict[str, str]]:
    # Достаём из ОЗУ (на проде у тебя есть Postgres — можешь обогатить контекст диалогом)
    mem = RAM_MEMORY.get(user_id, {})
    user_name = mem.get("name") or username or "друг"
    fav = mem.get("drinks", [])
    summary = mem.get("summary", "")

    context = f"Имя пользователя: {user_name}. Любимые напитки: {', '.join(fav) if fav else 'не уточнял'}. "
    if summary:
        context += f"Краткое резюме прошлых диалогов: {summary}."

    sys = SYSTEM_PROMPT + " " + context

    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": text},
    ]


def update_memory(user_id: int, text: str, reply: str, user_name: Optional[str]):
    mem = RAM_MEMORY.setdefault(user_id, {"history": []})
    if user_name and not mem.get("name"):
        mem["name"] = user_name
    mem["history"].append({"u": text, "a": reply})
    # Примитивное выделение любимых напитков
    lowered = text.lower()
    drinks = mem.setdefault("drinks", [])
    for k in ["вино", "пиво", "виски", "ром", "коньяк", "текила", "водка", "шампанское", "джин", "мохито", "маргарита", "негрони"]:
        if k in lowered and k not in drinks:
            drinks.append(k)
    # Сводка (обрежем историю)
    mem["history"] = mem["history"][-25:]


# ----------- МОДЕЛИ ДЛЯ ДЕБАГА ----------- #
class WebhookPing(BaseModel):
    ok: bool = True


# ----------- РОУТЫ ----------- #
@app.on_event("startup")
async def on_startup():
    log.info("✅ OpenAI client initialized")
    log.info("✅ Database initialized (если используешь Postgres, см. миграции)")
    # Автоматическая установка вебхука на старте (если есть переменные)
    if BOT_TOKEN and APP_BASE_URL:
        hook_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{TELEGRAM_API}/setWebhook", data={"url": hook_url, "allowed_updates": json.dumps(["message"])})
                log.info("✅ Webhook set to %s", hook_url)
                log.info("Telegram setWebhook %s %s", r.status_code, r.text)
        except Exception as e:
            log.exception("setWebhook error: %s", e)


@app.get("/")
async def root():
    return PlainTextResponse("OK", status_code=200)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok", "webhook": f"/webhook/{BOT_TOKEN[-6:] if BOT_TOKEN else 'no-token'}"})


@app.get("/webhook/{token}")
async def webhook_get(token: str):
    # Помогает понять, не шлёт ли Telegram GET по ошибке
    log.warning("GET /webhook/%s (Telegram должен слать POST)", token[:6] + "…")
    return JSONResponse({"ok": True, "hint": "Telegram должен POST-ить апдейты"}, status_code=200)


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", errors="replace")
    # Подробные логи входа
    log.info("Incoming webhook: method=%s path=%s token_match=%s",
             request.method, request.url.path, token == (BOT_TOKEN or ""))
    log.debug("Headers: %s", dict(request.headers))
    log.info("Body (first 2KB): %s", body_text[:2000])

    # Жёсткая проверка токена
    if not BOT_TOKEN or token != BOT_TOKEN:
        log.error("❌ Token mismatch in webhook URL")
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    # Парсим update
    try:
        update = json.loads(body_text) if body_text else {}
    except Exception as e:
        log.exception("JSON parse error: %s", e)
        return Response(status_code=200)

    # Извлекаем базовые поля
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text") or ""
    from_user = message.get("from") or {}
    username = from_user.get("first_name") or from_user.get("username")

    if not chat_id:
        log.warning("No chat_id in update")
        return Response(status_code=200)

    # Команды
    if text.startswith("/start"):
        greet = "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
        await tg_send_message(chat_id, greet)
        return Response(status_code=200)

    if text.startswith("/toast"):
        toast = "За встречу и искренние разговоры — чтобы бокалы пустели, а душа наполнялась! 🥂"
        await tg_send_message(chat_id, toast)
        return Response(status_code=200)

    # Обычное сообщение → OpenAI (или fallback)
    try:
        messages = build_messages(chat_id, username, text)
        reply = await openai_chat(messages)
    except Exception as e:
        log.exception("AI pipeline error: %s", e)
        reply = FALLBACK_REPLY

    # Сохраняем в память (в RAM и/или в твою БД, если включена)
    try:
        update_memory(chat_id, text, reply, username)
        # тут можно вызвать сохранение в Postgres, если уже готово
    except Exception as e:
        log.exception("Memory save error (RAM/DB): %s", e)

    # Отправляем ответ
    await tg_send_message(chat_id, reply)
    return Response(status_code=200)


# ----------- УТИЛИТНЫЕ ДЕБАГ-РОУТЫ ----------- #
@app.get("/debug/webhook-info")
async def debug_webhook_info():
    if not TELEGRAM_API:
        return JSONResponse({"error": "no BOT_TOKEN"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{TELEGRAM_API}/getWebhookInfo")
            return JSONResponse(r.json())
    except Exception as e:
        log.exception("getWebhookInfo error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/debug/memory/{chat_id}")
async def debug_memory(chat_id: int):
    return JSONResponse(RAM_MEMORY.get(chat_id, {}))

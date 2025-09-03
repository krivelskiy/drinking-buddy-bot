import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse, JSONResponse

# --- ЛОГИ ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("app")

# --- ENV ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("POSTGRESQL_URL")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

FALLBACK_REPLY = "Эх, давай просто выпьем за всё хорошее! 🥃"
DB_DOWN_REPLY = os.getenv(
    "DB_DOWN_REPLY",
    "Сегодня я без своей долгой памяти — давай просто выпьем за хорошее настроение! 🥂"
)

# --- СИСТЕМНЫЕ ПРАВИЛА ДЛЯ КАТИ ---
SYSTEM_PROMPT_BASE = (
    "Ты Катя — лёгкая, тёплая собеседница и собутыльница. "
    "Флиртуешь уместно, шутишь, поддерживаешь как психолог, ведёшь диалог непринуждённо. "
    "Ты помнишь имя и любимые напитки собеседника и подстраиваешься под них. "
    "Очень важно: не начинай ответы со стандартных приветствий и не обращайся по имени каждый раз, "
    "если разговор уже идёт. Отвечай по делу, естественно и короткими абзацами. "
    "Если пользователь спрашивает о своём любимом напитке — используй сохранённую память. "
    "Если просит тост — дай тёплый, небанальный тост. Всегда старайся завершать реплику вопросом, "
    "чтобы поддержать беседу."
)

# --- АВАРИЙНАЯ RAM-ПАМЯТЬ (если БД недоступна) ---
RAM: Dict[int, Dict[str, Any]] = {}

# --- DB (SQLAlchemy sync) ---
DB_AVAILABLE = False
DB_PROBE_ERROR = None

def db_init() -> bool:
    """Создаёт таблицы при необходимости. Возвращает True, если БД доступна."""
    global DB_PROBE_ERROR
    if not DATABASE_URL:
        DB_PROBE_ERROR = "DATABASE_URL not set"
        return False
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            name TEXT,
            favorite_drinks JSONB DEFAULT '[]'::jsonb,
            summary TEXT,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            role TEXT NOT NULL,          -- 'user' | 'assistant'
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        DB_PROBE_ERROR = f"init error: {e}"
        return False

def db_get_user(chat_id: int) -> Dict[str, Any]:
    try:
        import psycopg2, psycopg2.extras  # type: ignore
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT chat_id, name, favorite_drinks, summary FROM users WHERE chat_id=%s;", (chat_id,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users(chat_id) VALUES(%s) ON CONFLICT DO NOTHING;",
                (chat_id,)
            )
            conn.commit()
            row = {"chat_id": chat_id, "name": None, "favorite_drinks": [], "summary": None}
        else:
            if row.get("favorite_drinks") is None:
                row["favorite_drinks"] = []
        cur.close()
        conn.close()
        return row
    except Exception as e:
        log.exception("db_get_user error: %s", e)
        return {"chat_id": chat_id, "name": None, "favorite_drinks": [], "summary": None}

def db_update_user(chat_id: int, name: Optional[str] = None, add_drinks: Optional[List[str]] = None):
    try:
        import psycopg2, psycopg2.extras  # type: ignore
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        if name is not None:
            cur.execute("UPDATE users SET name=%s, updated_at=now() WHERE chat_id=%s;", (name, chat_id))
        if add_drinks:
            # аккуратно мёржим список
            cur.execute("SELECT favorite_drinks FROM users WHERE chat_id=%s;", (chat_id,))
            row = cur.fetchone()
            cur_drinks = row[0] if row and row[0] else []
            merged = list(dict.fromkeys([*cur_drinks, *add_drinks]))
            cur.execute(
                "UPDATE users SET favorite_drinks=%s, updated_at=now() WHERE chat_id=%s;",
                (json.dumps(merged, ensure_ascii=False), chat_id),
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.exception("db_update_user error: %s", e)

def db_save_message(chat_id: int, role: str, content: str):
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages(chat_id, role, content) VALUES (%s, %s, %s);",
            (chat_id, role, content),
        )
        # оставляем последние 50 сообщений
        cur.execute("""
            DELETE FROM messages
            WHERE chat_id=%s AND id NOT IN (
                SELECT id FROM messages WHERE chat_id=%s ORDER BY id DESC LIMIT 50
            );
        """, (chat_id, chat_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.exception("db_save_message error: %s", e)

def db_get_recent_dialogue(chat_id: int, limit_pairs: int = 8) -> List[Tuple[str, str]]:
    """Возвращает последние пары (user, assistant) как список пар строк.
       Если последовательность не парная, берём последние сообщения по порядку."""
    try:
        import psycopg2, psycopg2.extras  # type: ignore
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT role, content
            FROM messages
            WHERE chat_id=%s
            ORDER BY id DESC
            LIMIT %s;
        """, (chat_id, limit_pairs * 2))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        rows = list(reversed(rows))
        # возьмём только последние сообщения (user/assistant) без строгой парности
        return [(r["role"], r["content"]) for r in rows]
    except Exception as e:
        log.exception("db_get_recent_dialogue error: %s", e)
        return []

# --- TG + OpenAI ---
async def tg_send_message(chat_id: int, text: str) -> Optional[dict]:
    if not TELEGRAM_API:
        log.error("❌ TELEGRAM_API not configured")
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})
            log.info("TG sendMessage %s %s", r.status_code, r.text[:400])
            return r.json()
    except Exception as e:
        log.exception("TG sendMessage error: %s", e)
        return None

def _extract_drinks_freeform(text: str) -> List[str]:
    """Выдёргиваем напитки из произвольных фраз — в т.ч. составные (‘барнаульское чешское’)."""
    s = text.lower()
    # если встречается "мой любимый", "любимый сорт/напиток" — берём всё после тире/двоеточия/слова "—/ - /: "
    m = re.search(r"(любим(ый|ое|ая).{0,20}?(сорт|напиток)[^\wа-яё]+)(.+)$", s)
    candidates: List[str] = []
    if m:
        tail = m.group(4).strip()
        # отрезаем лишнее после точки/восклиц / вопроса
        tail = re.split(r"[.?!]", tail)[0].strip()
        if tail:
            candidates.append(tail)
    # словарь популярных напитков (если просто упомянули)
    vocab = [
        "вино","пиво","виски","ром","коньяк","текила","водка","шампанское","джин",
        "негрони","апероль","маргарита","мохито","манхэттен","олд фэшнд","барнаульское чешское"
    ]
    for v in vocab:
        if v in s and v not in candidates:
            candidates.append(v)
    # нормализуем пробелы
    clean = []
    for c in candidates:
        c = re.sub(r"\s+", " ", c).strip(" -–—:;,.!?'\"").strip()
        if c:
            clean.append(c)
    # уникализируем с сохранением порядка
    unique = list(dict.fromkeys(clean))
    return unique[:5]

async def openai_chat(messages: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY absent -> fallback")
        return FALLBACK_REPLY
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.7, "max_tokens": 380}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(OPENAI_CHAT_URL, headers=headers, json=payload)
            log.info("OpenAI %s: %s", r.status_code, r.text[:600])
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
            log.error("OpenAI error %s: %s", r.status_code, r.text)
            return FALLBACK_REPLY
    except Exception as e:
        log.exception("OpenAI exception: %s", e)
        return FALLBACK_REPLY

def build_messages_with_memory(chat_id: int, incoming_text: str, user_hint_name: Optional[str]) -> List[Dict[str, str]]:
    """Собираем системный промпт + недавний контекст из БД (или RAM), + текущий запрос."""
    # БД/или RAM
    if DB_AVAILABLE:
        u = db_get_user(chat_id)
        name = u.get("name") or user_hint_name
        drinks = u.get("favorite_drinks") or []
        summary = u.get("summary") or ""
        history_rows = db_get_recent_dialogue(chat_id, limit_pairs=8)
    else:
        mem = RAM.get(chat_id, {})
        name = mem.get("name") or user_hint_name
        drinks = mem.get("drinks", [])
        summary = mem.get("summary", "")
        history_rows = mem.get("history", [])

    # системный
    context = f"Имя пользователя: {name or 'не сказал'}. Любимые напитки: "
    context += (", ".join(drinks) if drinks else "пока не рассказаны") + ". "
    if summary:
        context += f"Краткое резюме прошлых диалогов: {summary}."

    sys = f"{SYSTEM_PROMPT_BASE} {context}"

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]

    # недавняя история
    if history_rows:
        # history_rows может быть [("user","..."),("assistant","..."),...]
        for r in history_rows[-16:]:
            role, content = (r if isinstance(r, tuple) else (r.get("role"), r.get("content")))
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    # текущий запрос
    messages.append({"role": "user", "content": incoming_text})
    return messages

def update_memory(chat_id: int, text: str, reply: str, user_name: Optional[str]):
    drinks_found = _extract_drinks_freeform(text)
    if DB_AVAILABLE:
        if user_name:
            db_update_user(chat_id, name=user_name)
        if drinks_found:
            db_update_user(chat_id, add_drinks=drinks_found)
        db_save_message(chat_id, "user", text)
        db_save_message(chat_id, "assistant", reply)
    else:
        # RAM fallback
        mem = RAM.setdefault(chat_id, {"history": [], "drinks": []})
        if user_name and not mem.get("name"):
            mem["name"] = user_name
        if drinks_found:
            mem["drinks"] = list(dict.fromkeys([*mem["drinks"], *drinks_found]))
        mem["history"].append(("user", text))
        mem["history"].append(("assistant", reply))
        mem["history"] = mem["history"][-20:]

# --- FastAPI ---
app = FastAPI(title="Drinking Buddy Bot")

@app.on_event("startup")
async def startup():
    global DB_AVAILABLE
    # DB
    DB_AVAILABLE = db_init()
    if DB_AVAILABLE:
        log.info("✅ Database initialized and available")
    else:
        log.warning("⚠️ Database unavailable: %s", DB_PROBE_ERROR or "unknown")

    # setWebhook
    if BOT_TOKEN and APP_BASE_URL:
        hook_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{TELEGRAM_API}/setWebhook",
                    data={"url": hook_url, "allowed_updates": json.dumps(["message"])}
                )
            log.info("✅ Webhook set to %s", hook_url)
            log.info("TG setWebhook %s %s", r.status_code, r.text)
        except Exception as e:
            log.exception("setWebhook error: %s", e)

@app.get("/")
async def root():
    return PlainTextResponse("OK", status_code=200)

@app.get("/healthz")
async def health():
    return JSONResponse({
        "ok": True,
        "db_available": DB_AVAILABLE,
        "db_reason": DB_PROBE_ERROR,
        "app_base_url": APP_BASE_URL,
        "webhook": f"/webhook/{(BOT_TOKEN or 'no-token')[-6:]}"
    })

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    body = await request.body()
    text_body = body.decode("utf-8", errors="replace")
    log.info("Incoming webhook token_ok=%s body=%s", token == (BOT_TOKEN or ""), text_body[:1800])

    if not BOT_TOKEN or token != BOT_TOKEN:
        log.error("❌ Token mismatch")
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    try:
        update = json.loads(text_body) if text_body else {}
    except Exception as e:
        log.exception("JSON parse error: %s", e)
        return Response(status_code=200)

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""
    from_user = msg.get("from") or {}
    first_name = from_user.get("first_name")
    username = from_user.get("username")
    name_hint = first_name or username

    if not chat_id:
        return Response(status_code=200)

    # команды (всегда работают)
    if text.startswith("/start"):
        greet = "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
        await tg_send_message(chat_id, greet)
        return Response(status_code=200)

    if text.startswith("/toast"):
        toast = "За встречу и искренние разговоры — чтобы бокалы пустели, а душа наполнялась! 🥂"
        await tg_send_message(chat_id, toast)
        return Response(status_code=200)

    # если БД недоступна — фиксированный ответ
    if not DB_AVAILABLE:
        await tg_send_message(chat_id, DB_DOWN_REPLY)
        return Response(status_code=200)

    # обычный ход: собираем контекст с памятью и спрашиваем ИИ
    try:
        messages = build_messages_with_memory(chat_id, text, name_hint)
        reply = await openai_chat(messages)
    except Exception as e:
        log.exception("AI pipeline error: %s", e)
        reply = FALLBACK_REPLY

    # сохраняем память (имя/напитки/история)
    try:
        update_memory(chat_id, text, reply, name_hint)
    except Exception as e:
        log.exception("memory update error: %s", e)

    await tg_send_message(chat_id, reply)
    return Response(status_code=200)

# Диагностика
@app.get("/debug/webhook-info")
async def webhook_info():
    if not TELEGRAM_API:
        return JSONResponse({"error": "no BOT_TOKEN"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{TELEGRAM_API}/getWebhookInfo")
        return JSONResponse(r.json())
    except Exception as e:
        log.exception("getWebhookInfo error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/debug/user/{chat_id}")
async def debug_user(chat_id: int):
    if DB_AVAILABLE:
        return JSONResponse(db_get_user(chat_id))
    return JSONResponse(RAM.get(chat_id, {}))

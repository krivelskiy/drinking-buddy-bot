import os
import json
import logging
import random
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from dotenv import load_dotenv

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("app")

# --- Env ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # postgres://...  | sqlite:///...
BOT_NAME = os.getenv("BOT_NAME", "Катя Собутыльница")

if not BOT_TOKEN or not APP_BASE_URL:
    log.warning("BOT_TOKEN or APP_BASE_URL is empty. Webhook won't be set.")

# --- OpenAI (опционально используется в вашем коде ответа ИИ) ---
try:
    from openai import OpenAI
    oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    log.info("✅ OpenAI client initialized")
except Exception as e:
    oai = None
    log.exception("OpenAI init failed: %s", e)

# --- SQLAlchemy ---
from sqlalchemy import (
    create_engine, Integer, String, DateTime, Text, func, Column
)
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()

class UserMemory(Base):
    __tablename__ = "user_memory"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, unique=True, nullable=False)
    name = Column(String(255))
    favorite_drink = Column(String(255))
    history = Column(Text)                         # JSONL
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

class GiftTransaction(Base):
    __tablename__ = "gift_transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    gift_code = Column(String(64), nullable=False)         # beer | wine | whiskey | vodka | champagne
    title = Column(String(255), nullable=False)            # Название подарка
    amount_stars = Column(Integer, nullable=False)         # Сколько Stars заплатили
    payload = Column(String(255), nullable=False)          # tg_payload, для сверки
    created_at = Column(DateTime, default=func.now())

# DB init
ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=ENGINE, autocommit=False, autoflush=False)
try:
    Base.metadata.create_all(ENGINE)
    log.info("✅ Database initialized")
except Exception as e:
    log.exception("❌ Database init failed: %s", e)

# --- Telegram PTB v20 ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters
)

# Создаём Application один раз
tapp: Application = ApplicationBuilder().token(BOT_TOKEN).build()

# --- Стикеры по напиткам ---
STICKERS = {
    "beer": "CAACAgIAAxkBAAEBjsRouGBy8fdkWj0MhodvqLl3eT9fcgACX4cAAvmhwElmpyDuoHw7IjYE",
    "wine": "CAACAgIAAxkBAAEBjsJouGBk6eEZ60zhrlVYxtaa6o1IpwACzoEAApg_wUm0xElTR8mU3zYE",
    "whiskey": "CAACAgIAAxkBAAEBjsBouGBSGJX2UPfsKzHTIYlfD7eAswACDH8AAnEbyEnqwlOYBHZL3jYE",
    "vodka": "CAACAgIAAxkBAAEBjr5ouGBBx_1-DTY7HwkdW3rQWOcgRAACsIAAAiFbyEn_G4lgoMu7IjYE",
    "happy": "CAACAgIAAxkBAAEBjrpouGAERwa1uHIJiB5lkhQZps-j_wACcoEAAlGlwEnCOTC-IwMCBDYE",
    "sad": "CAACAgIAAxkBAAEBjrxouGAyqkcwuIJiCaINHEu-QVn4NAAC1IAAAhynyUnZmmKvP768xzYE",
    "champagne": None,  # добавьте file_id, когда загрузите
}

# --- «Магазин подарков» (Stars) ---
# provider_token для Stars должен быть пустым
PROVIDER_TOKEN_STARS = ""      # ВАЖНО: пустая строка для XTR
CURRENCY = "XTR"

# КАЖДЫЙ подарок = 1⭐ (по твоей задаче)
GIFTS = {
    # code: (title, amount_in_stars, sticker_key)
    "beer": ("Бокал пива для Кати", 1, "beer"),
    "wine": ("Бокал вина для Кати", 1, "wine"),
    "whiskey": ("Шот виски для Кати", 1, "whiskey"),
    "vodka": ("Стопка водки для Кати", 1, "vodka"),
    "champagne": ("Бокал шампанского для Кати", 1, "champagne"),
}

# --- Память/история ---
def _append_history(mem: UserMemory, role: str, text: str):
    # Храним «JSON Lines»
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "text": text
    }, ensure_ascii=False)
    existed = (mem.history or "").strip()
    mem.history = (existed + "\n" if existed else "") + line

def _get_or_create_memory(session, user_id: int, user_name: str) -> UserMemory:
    try:
        mem = session.query(UserMemory).filter_by(user_id=user_id).first()
    except Exception:
        session.rollback()
        mem = None
    if not mem:
        mem = UserMemory(user_id=user_id, name=user_name, favorite_drink=None, history=None)
        session.add(mem)
        session.commit()
    return mem

def _save_message(session, user_id: int, user_name: str, text: str) -> UserMemory:
    mem = _get_or_create_memory(session, user_id, user_name)
    _append_history(mem, "user", text)
    session.commit()
    return mem

def _save_bot_reply(session, mem: UserMemory, text: str):
    _append_history(mem, "assistant", text)
    session.commit()

def _detect_drink(text: str) -> str | None:
    t = (text or "").lower()
    if any(w in t for w in ["пиво", "beer", "lager", "эйл"]):
        return "beer"
    if any(w in t for w in ["вино", "wine", "мерло", "каберне"]):
        return "wine"
    if any(w in t for w in ["виски", "whisky", "whiskey", "бурбон", "скотч"]):
        return "whiskey"
    if any(w in t for w in ["водка", "vodka"]):
        return "vodka"
    if any(w in t for w in ["шампан", "champagne", "просекко", "кава"]):
        return "champagne"
    return None

def _should_katya_initiate_toast() -> bool:
    return random.random() < 0.15

# --- Ответы ИИ (заглушка) ---
async def generate_reply(user_text: str, mem: UserMemory) -> str:
    base = "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
    if not user_text:
        return base
    fav = mem.favorite_drink or "что-нибудь вкусное"
    return f"Хороший выбор! Я вообще люблю поболтать за {fav}. О чём поговорим?"

# --- TG Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        uid = user.id if user else 0
        name = user.full_name if user else "Гость"
        args = context.args or []
        with SessionLocal() as s:
            _get_or_create_memory(s, uid, name)
        if args and args[0] == "gift":
            await gift_command(update, context)
            return
        await update.effective_chat.send_message(
            "Привет! Я Катя. А как тебя зовут и что ты сегодня хочешь выпить?"
        )
    except Exception as e:
        log.exception("start handler failed: %s", e)

def _gift_keyboard():
    buttons = []
    row = []
    for code, (title, amount, _) in GIFTS.items():
        row.append(InlineKeyboardButton(f"{title} · {amount}⭐", callback_data=f"gift:{code}"))
        if len(row) == 1:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

async def gift_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            await update.message.reply_text("Выбери подарок для Кати:", reply_markup=_gift_keyboard())
        elif update.callback_query:
            await update.callback_query.message.reply_text("Выбери подарок для Кати:", reply_markup=_gift_keyboard())
    except Exception as e:
        log.exception("gift_command failed: %s", e)

async def gift_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        data = (query.data or "")
        if not data.startswith("gift:"):
            return
        code = data.split(":", 1)[1]
        if code not in GIFTS:
            await query.edit_message_text("Такого подарка нет 😔")
            return
        title, amount, _ = GIFTS[code]
        payload = f"gift:{code}:{int(datetime.now().timestamp())}"
        prices = [LabeledPrice(label=title, amount=amount)]
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=title,
            description=f"Виртуальный подарок для Кати — {title}",
            payload=payload,
            provider_token=PROVIDER_TOKEN_STARS,  # ПУСТО для Stars
            currency=CURRENCY,                    # XTR
            prices=prices,
            start_parameter=f"gift_{code}",
            need_name=False,
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            is_flexible=False,
        )
    except Exception as e:
        log.exception("gift_select failed: %s", e)

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.pre_checkout_query
        ok = True
        err = None

        payload = q.invoice_payload or ""
        if not (payload.startswith("gift:") and len(payload.split(":")) >= 2):
            ok, err = False, "Неверные данные платежа"
        else:
            code = payload.split(":")[1]
            if code not in GIFTS:
                ok, err = False, "Неизвестный подарок"
            else:
                expected_amount = GIFTS[code][1]
                if q.currency != CURRENCY or q.total_amount != expected_amount:
                    ok, err = False, "Сумма или валюта не совпадает"

        await context.bot.answer_pre_checkout_query(
            pre_checkout_query_id=q.id,
            ok=ok,
            error_message=err if not ok else None
        )
    except Exception as e:
        log.exception("precheckout_handler failed: %s", e)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sp = update.message.successful_payment
        user = update.effective_user
        uid = user.id if user else 0
        name = user.full_name if user else "Гость"

        payload = sp.invoice_payload or ""
        amount = sp.total_amount or 0
        code = "unknown"
        if payload.startswith("gift:"):
            parts = payload.split(":")
            if len(parts) >= 2:
                code = parts[1]

        title, _, sticker_key = GIFTS.get(code, (f"Подарок ({code})", amount, "happy"))

        # сохраняем транзакцию
        with SessionLocal() as s:
            _get_or_create_memory(s, uid, name)
            gt = GiftTransaction(
                user_id=uid, gift_code=code, title=title,
                amount_stars=amount, payload=payload
            )
            s.add(gt)
            s.commit()

        # благодарность + тост + стикер
        thanks = f"Спасибо за подарок! *{title}* — это так мило 🥰\n" \
                 f"Поднимаю бокал за тебя! За щедрость и за классное настроение! 🥂"
        await update.message.reply_text(thanks, parse_mode="Markdown")

        st_id = STICKERS.get(sticker_key)
        if st_id:
            await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=st_id)
        else:
            if STICKERS.get("happy"):
                await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=STICKERS["happy"])

        log.info("Gift received: user=%s code=%s stars=%s payload=%s", uid, code, amount, payload)
    except Exception as e:
        log.exception("successful_payment_handler failed: %s", e)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Основной обработчик текста: сохраняет историю, реагирует на напитки,
    иногда сама инициирует тост со стикером, и генерит общий ответ.
    """
    try:
        if not update.message:
            return
        user = update.effective_user
        uid = user.id if user else 0
        name = user.full_name if user else "Гость"
        text = update.message.text or ""

        # Сохраним историю
        with SessionLocal() as s:
            mem = _save_message(s, uid, name, text)

            low = text.lower()
            if "мой любимый" in low and ("пиво" in low or "вино" in low or "виски" in low or "водк" in low or "шампан" in low):
                fav = text.split("—", 1)[-1].strip() if "—" in text else (
                    text.split(":", 1)[-1].strip() if ":" in text else None
                )
                mem.favorite_drink = fav or mem.favorite_drink or "без уточнений"
                s.commit()
                await update.message.reply_text(f"Запомнила! Твой любимый напиток: {mem.favorite_drink}")

            drink = _detect_drink(text)
            if drink:
                st_id = STICKERS.get(drink)
                if st_id:
                    await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=st_id)
                await update.message.reply_text("За нас! 🍻 Давай поддерживать хорошее настроение!")

            if not drink and _should_katya_initiate_toast():
                rnd = random.choice(["beer", "wine", "whiskey", "vodka"])
                st = STICKERS.get(rnd)
                if st:
                    await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=st)
                await update.message.reply_text("Я первая поднимаю бокал! 🥂 Тост за приятную беседу!")

            reply = await generate_reply(text, mem)
            await update.message.reply_text(reply)
            _save_bot_reply(s, mem, reply)

    except Exception as e:
        log.exception("handle_message failed: %s", e)
        try:
            await update.effective_chat.send_message(
                "У меня небольшая заминка 🙈 Попробуй повторить, а я всё быстро починю."
            )
        except Exception:
            pass

# --- FastAPI + Webhook ---

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    try:
        await tapp.initialize()

        tapp.add_handler(CommandHandler("start", start))
        tapp.add_handler(CommandHandler("gift", gift_command))
        tapp.add_handler(CallbackQueryHandler(gift_select, pattern=r"^gift:"))
        tapp.add_handler(PreCheckoutQueryHandler(precheckout_handler))
        tapp.add_handler(MessageHandler(filters.StatusUpdate.SUCCESSFUL_PAYMENT, successful_payment_handler))
        tapp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        if BOT_TOKEN and APP_BASE_URL:
            wh_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
            await tapp.bot.set_webhook(url=wh_url, allowed_updates=["message", "callback_query", "pre_checkout_query"])
            log.info("✅ Webhook set to %s", wh_url)
        else:
            log.warning("Webhook not set (no BOT_TOKEN/APP_BASE_URL)")
    except Exception as e:
        log.exception("Startup failed: %s", e)

@app.get("/", response_class=PlainTextResponse)
async def health():
    return "ok"

@app.post("/webhook/{token}", response_class=PlainTextResponse)
async def telegram_webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        return PlainTextResponse("forbidden", status_code=403)
    try:
        data = await request.json()
        upd_id = data.get("update_id")
        log.info("Incoming update_id=%s", upd_id)
        update = Update.de_json(data=data, bot=tapp.bot)
        await tapp.process_update(update)
        return "ok"
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return "ok"

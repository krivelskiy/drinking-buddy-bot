# app.py
import os
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple, List

from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv

import sqlalchemy as sa
from sqlalchemy import String, BigInteger, Integer, DateTime, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice,
)
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, PreCheckoutQueryHandler,
)

# -----------------------------------------------------------------------------
# Конфиг и логирование
# -----------------------------------------------------------------------------
load_dotenv()

log = logging.getLogger("app")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ✅ Читаем токен из любого привычного имени
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TELEGRAM_API_TOKEN")
    or ""
)

APP_BASE_URL = os.getenv("APP_BASE_URL", "")  # например: https://drinking-buddy-bot.onrender.com
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./db.sqlite3")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")

if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY is empty")
if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty (webhook/бот работать не будет)")
if not APP_BASE_URL:
    log.warning("APP_BASE_URL is empty (webhook не будет установлен)")

# -----------------------------------------------------------------------------
# БД (SQLAlchemy)
# -----------------------------------------------------------------------------
engine = sa.create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = sa.Column(Integer, primary_key=True)
    chat_id = sa.Column(BigInteger, nullable=False, index=True)
    username = sa.Column(String(255), nullable=True)
    first_name = sa.Column(String(255), nullable=True)
    last_name = sa.Column(String(255), nullable=True)
    created_at = sa.Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = sa.Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    transactions = relationship("GiftTransaction", back_populates="user", cascade="all, delete-orphan")


class GiftTransaction(Base):
    __tablename__ = "gift_transactions"
    id = sa.Column(Integer, primary_key=True)
    user_id = sa.Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tg_payment_charge_id = sa.Column(String(255), nullable=True)
    payload = sa.Column(String(255), nullable=True)
    total_amount = sa.Column(Integer, nullable=False, default=0)
    currency = sa.Column(String(10), nullable=False, default="XTR")
    status = sa.Column(String(32), nullable=False, default="pending")
    raw = sa.Column(JSON, nullable=True)

    created_at = sa.Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = sa.Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="transactions")


def init_db():
    Base.metadata.create_all(bind=engine)
    log.info("✅ Database initialized")


# -----------------------------------------------------------------------------
# OpenAI — заглушка, чтобы чат жил независимо от оплат
# -----------------------------------------------------------------------------
async def ask_llm(prompt: str) -> str:
    return f"🤖 {prompt}"


# -----------------------------------------------------------------------------
# Утилиты БД
# -----------------------------------------------------------------------------
def get_or_create_user(session, chat_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> User:
    user = session.execute(sa.select(User).where(User.chat_id == chat_id)).scalar_one_or_none()
    if user is None:
        user = User(
            chat_id=chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        session.add(user)
        session.flush()
        log.info("Created user chat_id=%s id=%s", chat_id, user.id)
    else:
        changed = False
        if user.username != username:
            user.username = username; changed = True
        if user.first_name != first_name:
            user.first_name = first_name; changed = True
        if user.last_name != last_name:
            user.last_name = last_name; changed = True
        if changed:
            session.flush()
    return user


# -----------------------------------------------------------------------------
# Магазин напитков за звезды
# -----------------------------------------------------------------------------
DRINKS: List[Tuple[str, str, int]] = [
    ("espresso", "Эспрессо ☕", 1),
    ("latte", "Латте 🥛☕", 1),
    ("beer", "Пиво 🍺", 1),
    ("cola", "Кола 🥤", 1),
]

def build_drinks_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for slug, title, price in DRINKS:
        rows.append([InlineKeyboardButton(f"{title} — {price}⭐", callback_data=f"buy:{slug}")])
    return InlineKeyboardMarkup(rows)

def find_drink(slug: str) -> Optional[Tuple[str, str, int]]:
    for d in DRINKS:
        if d[0] == slug:
            return d
    return None


# -----------------------------------------------------------------------------
# Telegram Bot
# -----------------------------------------------------------------------------
tapp: Optional[Application] = None
app = FastAPI()

class WebhookUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    edited_message: Optional[dict] = None
    channel_post: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    callback_query: Optional[dict] = None
    pre_checkout_query: Optional[dict] = None
    my_chat_member: Optional[dict] = None
    chat_member: Optional[dict] = None
    chat_join_request: Optional[dict] = None

# ----- Хендлеры
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return

    with SessionLocal() as session:
        get_or_create_user(
            session,
            chat_id=chat.id,
            username=update.effective_user.username if update.effective_user else None,
            first_name=update.effective_user.first_name if update.effective_user else None,
            last_name=update.effective_user.last_name if update.effective_user else None,
        )
        session.commit()

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Купить напиток (1⭐)", callback_data="open_shop")]])
    if update.message:
        await update.message.reply_text(
            "Привет! Я твой Drinking Buddy 🍻\nМожешь написать мне что-нибудь или купить напиток за звёзды.",
            reply_markup=kb
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "/start — начать\n"
            "/shop — магазин напитков за звезды (каждый по 1⭐)\n"
            "/ping — проверка живости\n"
            "Просто напиши сообщение — я отвечу 🙂"
        )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("pong ✅")

async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Выбери напиток:", reply_markup=build_drinks_keyboard())

async def open_shop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        await q.message.edit_text("Выбери напиток:", reply_markup=build_drinks_keyboard())

async def buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    slug = q.data.split(":", 1)[1] if q.data and ":" in q.data else ""
    drink = find_drink(slug)
    if not drink:
        await q.message.reply_text("Не нашёл такой напиток 🙈")
        return

    if not PAYMENT_PROVIDER_TOKEN:
        await q.message.reply_text("Оплата звёздами сейчас недоступна. Попробуй позже 🙏")
        return

    _, title, price_stars = drink
    prices = [LabeledPrice(label=title, amount=price_stars)]  # 1⭐

    payload = f"drink:{slug}"
    try:
        await q.message.reply_invoice(
            title=f"Покупка: {title}",
            description="Каждый напиток стоит 1⭐",
            payload=payload,
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
        )
    except Exception as e:
        log.exception("Failed to send invoice: %s", e)
        await q.message.reply_text("Не удалось создать счёт. Попробуй позже 🙏")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        await query.answer(ok=True)
    except Exception as e:
        log.exception("PreCheckout error: %s", e)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    sp = msg.successful_payment if msg else None
    chat = update.effective_chat

    payload = sp.invoice_payload if sp else None
    total_amount = sp.total_amount if sp else 0
    charge_id = sp.telegram_payment_charge_id if sp else None
    currency = sp.currency if sp else "XTR"

    with SessionLocal() as session:
        user = get_or_create_user(
            session,
            chat_id=chat.id,
            username=update.effective_user.username if update.effective_user else None,
            first_name=update.effective_user.first_name if update.effective_user else None,
            last_name=update.effective_user.last_name if update.effective_user else None,
        )
        session.add(GiftTransaction(
            user_id=user.id,
            tg_payment_charge_id=charge_id,
            payload=payload,
            total_amount=total_amount,
            currency=currency,
            status="successful",
            raw=msg.to_dict() if msg else None,
        ))
        session.commit()

    title = "напиток"
    if payload and payload.startswith("drink:"):
        slug = payload.split(":", 1)[1]
        d = find_drink(slug)
        if d:
            title = d[1]
    if msg:
        await msg.reply_text(f"Спасибо за покупку! {title} оформлен ✅")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "") if update.message else ""
    try:
        reply = await ask_llm(text)
    except Exception as e:
        log.exception("LLM error: %s", e)
        reply = "Ой, что-то пошло не так. Попробуй ещё раз 🙏"
    if update.message:
        await update.message.reply_text(reply)


def build_bot() -> Optional[Application]:
    if not BOT_TOKEN:
        # не падаем — просто вернём None; чат и оплаты не стартуют без токена
        return None

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("shop", shop_cmd))

    application.add_handler(CallbackQueryHandler(open_shop_cb, pattern="^open_shop$"))
    application.add_handler(CallbackQueryHandler(buy_cb, pattern="^buy:"))

    # Текст — в самом конце
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


# -----------------------------------------------------------------------------
# FastAPI + webhook
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    global tapp
    init_db()
    try:
        tapp = build_bot()

        if tapp:
            # Платежные хендлеры (не ломают старт, обрабатываются отдельно)
            tapp.add_handler(PreCheckoutQueryHandler(precheckout_handler))
            tapp.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

            # Ставим вебхук
            if APP_BASE_URL:
                wh_url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
                await tapp.bot.set_webhook(
                    url=wh_url,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                )
                log.info("✅ Webhook set to %s", wh_url)
            else:
                log.warning("Webhook not set: APP_BASE_URL is empty")
        else:
            log.error("Startup note: BOT_TOKEN is empty — бот не инициализирован.")
    except Exception as e:
        log.exception("Startup failed: %s", e)

@app.get("/", response_class=PlainTextResponse)
async def health():
    return "OK"

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    # если токен не задан — бот не поднят
    if not BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "bot token not configured"}, status_code=503)
    # защита: в пути должен быть именно токен бота
    if token != BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "wrong token"}, status_code=403)

    if not tapp:
        return JSONResponse({"ok": False, "error": "bot not started"}, status_code=503)

    data = await request.json()
    log.info("Incoming update_id=%s", data.get("update_id"))

    update = Update.de_json(data, tapp.bot)
    await tapp.process_update(update)
    return JSONResponse({"ok": True})

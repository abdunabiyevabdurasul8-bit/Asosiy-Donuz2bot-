import os
import json
import uuid
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import requests
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================================================
# ENV
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()          # platform/admin bot
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN sozlanmagan")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL sozlanmagan")
if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY sozlanmagan")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL sozlanmagan")

try:
    cipher = Fernet(ENCRYPTION_KEY.encode())
except Exception as e:
    raise RuntimeError("ENCRYPTION_KEY noto'g'ri") from e

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("multi-bot")

# Never log tokens/API keys or provider response bodies.
def safe_error(prefix: str, exc: Exception):
    log.error("%s (%s)", prefix, type(exc).__name__)

# ============================================================
# DB
# ============================================================
POOL = SimpleConnectionPool(
    1, 10,
    dsn=DATABASE_URL,
    sslmode="require"
)

def db():
    return POOL.getconn()

def db_put(conn):
    POOL.putconn(conn)

def q(sql, params=(), fetch=False, one=False, commit=False):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                rows = cur.fetchall()
                return rows[0] if one and rows else (None if one else rows)
        if commit:
            conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        db_put(conn)

def init_db():
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS platform_users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_instances (
                id UUID PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES platform_users(telegram_id),
                bot_token_enc TEXT NOT NULL,
                bot_username TEXT NOT NULL,
                bot_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                subscription_until TIMESTAMPTZ,
                suspended_at TIMESTAMPTZ,
                retention_until TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS bot_admins (
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                telegram_id BIGINT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(bot_id, telegram_id)
            );

            CREATE TABLE IF NOT EXISTS bot_users (
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                telegram_id BIGINT NOT NULL,
                username TEXT,
                first_name TEXT,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                total_deposited NUMERIC(18,2) NOT NULL DEFAULT 0,
                total_spent NUMERIC(18,2) NOT NULL DEFAULT 0,
                PRIMARY KEY(bot_id, telegram_id)
            );

            CREATE TABLE IF NOT EXISTS wallets (
                id BIGSERIAL PRIMARY KEY,
                owner_type TEXT NOT NULL CHECK(owner_type IN ('platform','bot_user')),
                platform_user_id BIGINT REFERENCES platform_users(telegram_id),
                bot_id UUID REFERENCES bot_instances(id) ON DELETE CASCADE,
                bot_user_id BIGINT,
                balance NUMERIC(18,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(owner_type, platform_user_id, bot_id, bot_user_id)
            );

            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id BIGSERIAL PRIMARY KEY,
                wallet_id BIGINT NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                amount NUMERIC(18,2) NOT NULL,
                type TEXT NOT NULL,
                note TEXT,
                admin_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id BIGSERIAL PRIMARY KEY,
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                owner_id BIGINT NOT NULL REFERENCES platform_users(telegram_id),
                plan TEXT NOT NULL,
                price NUMERIC(18,2) NOT NULL DEFAULT 0,
                starts_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ends_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY(bot_id, key)
            );

            CREATE TABLE IF NOT EXISTS api_credentials (
                id BIGSERIAL PRIMARY KEY,
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_enc TEXT NOT NULL,
                balance_url TEXT,
                catalog_url TEXT,
                order_url TEXT,
                markup_uzs NUMERIC(18,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS services (
                id BIGSERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS bot_services (
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                price NUMERIC(18,2) NOT NULL DEFAULT 0,
                provider TEXT,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                PRIMARY KEY(bot_id, service_id)
            );

            CREATE TABLE IF NOT EXISTS api_catalog (
                id BIGSERIAL PRIMARY KEY,
                api_id BIGINT NOT NULL REFERENCES api_credentials(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL,
                name TEXT NOT NULL,
                price NUMERIC(18,2) NOT NULL DEFAULT 0,
                raw_json JSONB,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(api_id, external_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id UUID PRIMARY KEY,
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                service_code TEXT NOT NULL,
                username TEXT,
                quantity NUMERIC(18,2),
                months INTEGER,
                provider_order_id TEXT,
                provider_cost NUMERIC(18,2) NOT NULL DEFAULT 0,
                sell_price NUMERIC(18,2) NOT NULL DEFAULT 0,
                delivery_type TEXT NOT NULL DEFAULT 'api',
                status TEXT NOT NULL DEFAULT 'pending',
                extra_data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS payments (
                id BIGSERIAL PRIMARY KEY,
                bot_id UUID,
                user_id BIGINT NOT NULL,
                amount NUMERIC(18,2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                receipt_file_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                approved_at TIMESTAMPTZ,
                admin_id BIGINT
            );

            CREATE TABLE IF NOT EXISTS promocodes (
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                amount NUMERIC(18,2) NOT NULL,
                limit_count INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(bot_id, code)
            );

            CREATE TABLE IF NOT EXISTS promo_uses (
                bot_id UUID NOT NULL REFERENCES bot_instances(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(bot_id, code, user_id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGSERIAL PRIMARY KEY,
                bot_id UUID,
                actor_id BIGINT,
                action TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            c.execute("""
            INSERT INTO services(code,name,category)
            VALUES
              ('stars','Telegram Stars','Stars'),
              ('premium','Telegram Premium','Premium'),
              ('sim','SIM','SIM'),
              ('donat','Donat','Donat'),
              ('hamkorlik','Hamkorlik','Hamkorlik')
            ON CONFLICT(code) DO NOTHING
            """)
        conn.commit()
    finally:
        db_put(conn)

# ============================================================
# SECURITY / MONEY
# ============================================================
def enc(value: str) -> str:
    return cipher.encrypt(value.encode()).decode()

def dec(value: str) -> str:
    try:
        return cipher.decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Saqlangan maxfiy ma'lumotni ochib bo'lmadi") from e

def mask_secret(secret: str) -> str:
    if not secret:
        return "••••"
    return ("••••••••" + secret[-4:]) if len(secret) > 4 else "••••"

def money(v):
    return Decimal(str(v)).quantize(Decimal("0.01"))

def register_platform(tg_user):
    q("""
    INSERT INTO platform_users(telegram_id,username,first_name,last_seen)
    VALUES(%s,%s,%s,NOW())
    ON CONFLICT(telegram_id) DO UPDATE SET
      username=EXCLUDED.username,
      first_name=EXCLUDED.first_name,
      last_seen=NOW()
    """, (tg_user.id, tg_user.username, tg_user.first_name), commit=True)

    # Platform wallet is independent from created bots.
    q("""
    INSERT INTO wallets(owner_type,platform_user_id,balance)
    VALUES('platform',%s,0)
    ON CONFLICT(owner_type,platform_user_id,bot_id,bot_user_id) DO NOTHING
    """, (tg_user.id,), commit=True)

def register_bot_user(bot_id, tg_user):
    q("""
    INSERT INTO bot_users(bot_id,telegram_id,username,first_name)
    VALUES(%s,%s,%s,%s)
    ON CONFLICT(bot_id,telegram_id) DO UPDATE SET
      username=EXCLUDED.username,
      first_name=EXCLUDED.first_name,
      last_seen=NOW()
    """, (str(bot_id), tg_user.id, tg_user.username, tg_user.first_name), commit=True)
    q("""
    INSERT INTO wallets(owner_type,bot_id,bot_user_id,balance)
    VALUES('bot_user',%s,%s,0)
    ON CONFLICT(owner_type,platform_user_id,bot_id,bot_user_id) DO NOTHING
    """, (str(bot_id), tg_user.id), commit=True)

def wallet_id(bot_id, user_id):
    row = q("""
      SELECT id FROM wallets
      WHERE owner_type='bot_user' AND bot_id=%s AND bot_user_id=%s
    """, (str(bot_id), user_id), fetch=True, one=True)
    return row[0] if row else None

def bot_balance(bot_id, user_id):
    row = q("""
      SELECT balance FROM wallets
      WHERE owner_type='bot_user' AND bot_id=%s AND bot_user_id=%s
    """, (str(bot_id), user_id), fetch=True, one=True)
    return Decimal(row[0]) if row else Decimal("0")

def change_bot_balance(bot_id, user_id, amount, tx_type, note, admin_id=None):
    amount = money(amount)
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute("""
              SELECT id,balance FROM wallets
              WHERE owner_type='bot_user' AND bot_id=%s AND bot_user_id=%s
              FOR UPDATE
            """, (str(bot_id), user_id))
            row = c.fetchone()
            if not row:
                raise ValueError("Wallet topilmadi")
            wid, bal = row
            new_bal = Decimal(bal) + amount
            if new_bal < 0:
                raise ValueError("Balans yetarli emas")
            c.execute("UPDATE wallets SET balance=%s WHERE id=%s", (new_bal, wid))
            c.execute("""
              INSERT INTO wallet_transactions(wallet_id,amount,type,note,admin_id)
              VALUES(%s,%s,%s,%s,%s)
            """, (wid, amount, tx_type, note, admin_id))
            if amount > 0:
                c.execute("""
                  UPDATE bot_users SET total_deposited=total_deposited+%s
                  WHERE bot_id=%s AND telegram_id=%s
                """, (amount, str(bot_id), user_id))
            elif tx_type == "purchase":
                c.execute("""
                  UPDATE bot_users SET total_spent=total_spent+%s
                  WHERE bot_id=%s AND telegram_id=%s
                """, (abs(amount), str(bot_id), user_id))
        conn.commit()
        return new_bal
    except Exception:
        conn.rollback()
        raise
    finally:
        db_put(conn)

# ============================================================
# BOT LOOKUPS
# ============================================================
def get_bot(bot_id):
    return q("""
      SELECT id,owner_id,bot_token_enc,bot_username,bot_name,status,
             subscription_until,retention_until
      FROM bot_instances WHERE id=%s
    """, (str(bot_id),), fetch=True, one=True)

def bot_is_active(bot_id):
    row = get_bot(bot_id)
    if not row:
        return False
    status = row[5]
    until = row[6]
    now = datetime.now(timezone.utc)
    return status == "active" and until and until > now

def is_bot_admin(bot_id, user_id):
    row = q("""
      SELECT 1 FROM bot_instances WHERE id=%s AND owner_id=%s
      UNION
      SELECT 1 FROM bot_admins WHERE bot_id=%s AND telegram_id=%s
      LIMIT 1
    """, (str(bot_id), user_id, str(bot_id), user_id), fetch=True, one=True)
    return bool(row)

def platform_admin(user_id):
    return user_id == ADMIN_ID

# ============================================================
# API HELPERS
# ============================================================
def api_request(method, url, token, payload=None):
    headers = {"Authorization": f"Bearer {token}", "X-API-Key": token}
    try:
        r = requests.request(
            method, url, headers=headers, json=payload, timeout=25
        )
        # Never log response body: it may contain secrets.
        if not r.ok:
            raise RuntimeError(f"Provider HTTP {r.status_code}")
        try:
            return r.json()
        except Exception:
            return {"raw": r.text[:1000]}
    except requests.RequestException as e:
        raise RuntimeError("Provider bilan ulanish xatosi") from e

def api_credentials(api_id):
    row = q("""
      SELECT id,bot_id,name,base_url,api_key_enc,balance_url,catalog_url,
             order_url,markup_uzs
      FROM api_credentials WHERE id=%s
    """, (api_id,), fetch=True, one=True)
    if not row:
        return None
    return {
        "id": row[0], "bot_id": row[1], "name": row[2], "base_url": row[3],
        "token": dec(row[4]), "balance_url": row[5], "catalog_url": row[6],
        "order_url": row[7], "markup": Decimal(row[8])
    }

def api_balance(api_id):
    a = api_credentials(api_id)
    if not a or not a["balance_url"]:
        raise RuntimeError("Balans endpoint sozlanmagan")
    return api_request("GET", a["balance_url"], a["token"])

# ============================================================
# TELEGRAM UI
# ============================================================
def user_kb():
    return ReplyKeyboardMarkup([
        ["💰 Balans", "📦 Buyurtma"],
        ["💳 Balans to‘ldirish", "📋 Buyurtmalarim"],
        ["👤 Profil", "❓ Yordam"]
    ], resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        ["👥 Foydalanuvchilar", "📦 Buyurtmalar"],
        ["➕ Balans qo‘shish", "📊 Statistika"],
        ["🔑 API", "⚙️ Sozlamalar"],
        ["⬅️ Menyu"]
    ], resize_keyboard=True)

def platform_kb():
    return ReplyKeyboardMarkup([
        ["🤖 Botlarim", "➕ Bot yaratish"],
        ["💰 Platforma balansi", "👥 Bot egalari"],
        ["📊 Platforma statistika"]
    ], resize_keyboard=True)

# ============================================================
# PLATFORM BOT
# ============================================================
async def platform_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_platform(update.effective_user)
    await update.message.reply_text(
        "Assalomu Aleykum!\n\nPlatforma boshqaruv botiga xush kelibsiz.",
        reply_markup=platform_kb() if platform_admin(update.effective_user.id)
        else ReplyKeyboardMarkup([["➕ Bot yaratish"],["🤖 Botlarim"]], resize_keyboard=True)
    )

async def platform_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_platform(update.effective_user)
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if text == "➕ Bot yaratish":
        context.user_data["creating_bot"] = True
        await update.message.reply_text(
            "BotFather bergan yangi bot TOKENini yuboring.\n"
            "Token saqlanganda bazada shifrlanadi va foydalanuvchilarga ko‘rsatilmaydi."
        )
        return

    if context.user_data.get("creating_bot"):
        token = text
        if len(token) < 20 or ":" not in token:
            await update.message.reply_text("❌ Token formati noto‘g‘ri.")
            return
        try:
            from telegram import Bot
            b = Bot(token)
            me = await b.get_me()
            bot_id = uuid.uuid4()
            q("""
              INSERT INTO bot_instances
              (id,owner_id,bot_token_enc,bot_username,bot_name,status,subscription_until)
              VALUES(%s,%s,%s,%s,%s,'active',%s)
            """, (
                str(bot_id), uid, enc(token), me.username or "",
                me.first_name or "", datetime.now(timezone.utc)+timedelta(days=7)
            ), commit=True)
            q("""
              INSERT INTO bot_admins(bot_id,telegram_id,role)
              VALUES(%s,%s,'owner')
              ON CONFLICT DO NOTHING
            """, (str(bot_id), uid), commit=True)
            context.user_data.pop("creating_bot", None)
            await update.message.reply_text(
                f"✅ @{me.username} yaratildi.\n"
                f"🆔 ID: {bot_id}\n"
                "7 kunlik sinov muddati berildi."
            )
            await RUNTIME.start_bot(str(bot_id))
        except Exception as e:
            safe_error("Bot yaratishda xato", e)
            await update.message.reply_text("❌ Token tekshirilmadi yoki botni ishga tushirib bo‘lmadi.")
        return

    if text == "🤖 Botlarim":
        rows = q("""
          SELECT bot_username,status,subscription_until,id
          FROM bot_instances WHERE owner_id=%s ORDER BY created_at DESC
        """, (uid,), fetch=True)
        if not rows:
            await update.message.reply_text("Sizda hali bot yo‘q.")
            return
        msg = ["🤖 Botlaringiz:"]
        for u, s, until, bid in rows:
            msg.append(f"@{u} | {s} | {until or '-'}\nID: {bid}")
        await update.message.reply_text("\n\n".join(msg))
        return

    if text == "💰 Platforma balansi":
        row = q("""
          SELECT balance FROM wallets
          WHERE owner_type='platform' AND platform_user_id=%s
        """, (uid,), fetch=True, one=True)
        await update.message.reply_text(f"💰 Platforma balansi: {row[0] if row else 0} so‘m")
        return

    if text == "📊 Platforma statistika" and platform_admin(uid):
        a = q("SELECT COUNT(*) FROM bot_instances", fetch=True, one=True)[0]
        u = q("SELECT COUNT(*) FROM platform_users", fetch=True, one=True)[0]
        bu = q("SELECT COUNT(*) FROM bot_users", fetch=True, one=True)[0]
        await update.message.reply_text(
            f"📊 Statistika\n\nBotlar: {a}\nBot egalari: {u}\nBot foydalanuvchilari: {bu}"
        )
        return

    if text == "👥 Bot egalari" and platform_admin(uid):
        rows = q("""
          SELECT telegram_id,username,first_name FROM platform_users
          ORDER BY created_at DESC LIMIT 100
        """, fetch=True)
        if not rows:
            await update.message.reply_text("Ma’lumot yo‘q.")
            return
        await update.message.reply_text(
            "\n".join(f"{r[0]} | @{r[1] or '-'} | {r[2] or '-'}" for r in rows)
        )
        return

    await update.message.reply_text("Menyudan foydalaning.", reply_markup=platform_kb())

# ============================================================
# CHILD BOT
# ============================================================
async def child_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    if not bot_is_active(bot_id):
        await update.message.reply_text("⏸ Bu botning obunasi tugagan.")
        return
    register_bot_user(bot_id, update.effective_user)
    await update.message.reply_text(
        "Assalomu Aleykum!\n\nXizmat botiga xush kelibsiz.",
        reply_markup=admin_kb() if is_bot_admin(bot_id, update.effective_user.id) else user_kb()
    )

async def child_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    admin = is_bot_admin(bot_id, update.effective_user.id)
    text = (
        "📚 Yordam\n"
        "/start — bosh menyu\n"
        "/balance — balans\n"
        "/profile — profil\n"
        "/orders — buyurtmalar\n"
    )
    if admin:
        text += "/admin — admin panel\n"
    await update.message.reply_text(text)

async def child_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    register_bot_user(bot_id, update.effective_user)
    bal = bot_balance(bot_id, update.effective_user.id)
    await update.message.reply_text(f"💰 Balans: {bal} so‘m")

async def child_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    register_bot_user(bot_id, update.effective_user)
    row = q("""
      SELECT username,first_name,first_seen,total_deposited,total_spent
      FROM bot_users WHERE bot_id=%s AND telegram_id=%s
    """, (str(bot_id), update.effective_user.id), fetch=True, one=True)
    await update.message.reply_text(
        f"👤 Profil\n\n"
        f"ID: {update.effective_user.id}\n"
        f"Username: @{row[0] or '-'}\n"
        f"Ism: {row[1] or '-'}\n"
        f"Balans: {bot_balance(bot_id, update.effective_user.id)} so‘m\n"
        f"Jami kiritilgan: {row[3]} so‘m\n"
        f"Jami sarflangan: {row[4]} so‘m"
    )

async def child_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    rows = q("""
      SELECT service_code,sell_price,status,created_at
      FROM orders WHERE bot_id=%s AND user_id=%s
      ORDER BY created_at DESC LIMIT 20
    """, (str(bot_id), update.effective_user.id), fetch=True)
    if not rows:
        await update.message.reply_text("📋 Buyurtmalar yo‘q.")
        return
    await update.message.reply_text(
        "\n".join(f"{r[0]} | {r[1]} so‘m | {r[2]} | {r[3]}" for r in rows)
    )

async def child_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    if not is_bot_admin(bot_id, update.effective_user.id):
        await update.message.reply_text("❌ Bu buyruq faqat bot egasi/admini uchun.")
        return
    await update.message.reply_text("👑 Admin panel", reply_markup=admin_kb())

async def child_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.application.bot_data["bot_id"]
    register_bot_user(bot_id, update.effective_user)
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    admin = is_bot_admin(bot_id, uid)

    if text == "💰 Balans":
        await child_balance(update, context)
        return
    if text == "👤 Profil":
        await child_profile(update, context)
        return
    if text == "📋 Buyurtmalarim":
        await child_orders(update, context)
        return
    if text == "❓ Yordam":
        await child_help(update, context)
        return

    if text == "📦 Buyurtma":
        await update.message.reply_text(
            "📦 Buyurtma\n\n"
            "Hozirgi versiyada buyurtma yozuvini yaratish uchun:\n"
            "1) xizmat nomini yuboring\n"
            "2) miqdorni yuboring\n\n"
            "Provider API ulangandan keyin auto-yetkazib berish shu buyurtmaga bog‘lanadi."
        )
        context.user_data["order_step"] = "service"
        return

    if context.user_data.get("order_step") == "service":
        context.user_data["order_service"] = text
        context.user_data["order_step"] = "amount"
        await update.message.reply_text("Miqdorni yuboring:")
        return

    if context.user_data.get("order_step") == "amount":
        try:
            amount = Decimal(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("❌ Miqdor noto‘g‘ri.")
            return

        service = context.user_data.get("order_service", "custom")
        # Demo/default price: amount itself. Real catalog price is loaded from api_catalog.
        price = money(amount)
        if bot_balance(bot_id, uid) < price:
            await update.message.reply_text("❌ Balans yetarli emas.")
            context.user_data.clear()
            return

        oid = uuid.uuid4()
        q("""
          INSERT INTO orders
          (id,bot_id,user_id,service_code,quantity,sell_price,delivery_type,status)
          VALUES(%s,%s,%s,%s,%s,%s,'api','pending')
        """, (str(oid), str(bot_id), uid, service, amount, price), commit=True)
        change_bot_balance(bot_id, uid, -price, "purchase", f"Order {oid}")
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Buyurtma qabul qilindi.\n"
            f"🆔 {oid}\n"
            f"💰 {price} so‘m\n"
            "⏳ Yetkazib berish provider API sozlamasiga bog‘liq."
        )
        return

    if text == "💳 Balans to‘ldirish":
        await update.message.reply_text(
            f"💳 Balans to‘ldirish\n\n"
            f"Karta: {CARD_NUMBER or 'Admin sozlamagan'}\n"
            f"Egasi: {CARD_OWNER or 'Admin sozlamagan'}\n\n"
            "To‘lovdan keyin chek/rasm yuboring."
        )
        return

    # Admin-only actions
    if admin and text == "👥 Foydalanuvchilar":
        rows = q("""
          SELECT telegram_id,username,first_name FROM bot_users
          WHERE bot_id=%s ORDER BY first_seen DESC LIMIT 100
        """, (str(bot_id),), fetch=True)
        if not rows:
            await update.message.reply_text("Foydalanuvchilar yo‘q.")
            return
        await update.message.reply_text(
            "\n".join(f"{r[0]} | @{r[1] or '-'} | {r[2] or '-'}" for r in rows)
        )
        return

    if admin and text == "📦 Buyurtmalar":
        rows = q("""
          SELECT id,user_id,service_code,sell_price,status,created_at
          FROM orders WHERE bot_id=%s ORDER BY created_at DESC LIMIT 50
        """, (str(bot_id),), fetch=True)
        if not rows:
            await update.message.reply_text("Buyurtmalar yo‘q.")
            return
        await update.message.reply_text(
            "\n".join(
                f"{r[0]} | {r[1]} | {r[2]} | {r[3]} so‘m | {r[4]}"
                for r in rows
            )
        )
        return

    if admin and text == "➕ Balans qo‘shish":
        context.user_data["admin_balance_step"] = "user"
        await update.message.reply_text("Foydalanuvchi Telegram ID sini yuboring:")
        return

    if admin and context.user_data.get("admin_balance_step") == "user":
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("❌ Telegram ID raqam bo‘lishi kerak.")
            return
        exists = q("""
          SELECT 1 FROM bot_users WHERE bot_id=%s AND telegram_id=%s
        """, (str(bot_id), target), fetch=True, one=True)
        if not exists:
            await update.message.reply_text("❌ Bu foydalanuvchi ushbu botda topilmadi.")
            return
        context.user_data["admin_balance_user"] = target
        context.user_data["admin_balance_step"] = "amount"
        await update.message.reply_text("Qancha so‘m qo‘shasiz? Masalan: 20000")
        return

    if admin and context.user_data.get("admin_balance_step") == "amount":
        try:
            amount = money(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
            target = context.user_data["admin_balance_user"]
            new_bal = change_bot_balance(
                bot_id, target, amount, "admin_add",
                "Admin balans qo‘shdi", uid
            )
            context.user_data.pop("admin_balance_step", None)
            context.user_data.pop("admin_balance_user", None)
            await update.message.reply_text(f"✅ Balans qo‘shildi.\nYangi balans: {new_bal} so‘m")
        except Exception:
            await update.message.reply_text("❌ Balans qo‘shishda xato.")
        return

    if admin and text == "📊 Statistika":
        users = q("SELECT COUNT(*) FROM bot_users WHERE bot_id=%s", (str(bot_id),), fetch=True, one=True)[0]
        orders = q("SELECT COUNT(*) FROM orders WHERE bot_id=%s", (str(bot_id),), fetch=True, one=True)[0]
        total = q("SELECT COALESCE(SUM(sell_price),0) FROM orders WHERE bot_id=%s", (str(bot_id),), fetch=True, one=True)[0]
        await update.message.reply_text(
            f"📊 Statistika\n\nFoydalanuvchilar: {users}\n"
            f"Buyurtmalar: {orders}\nAylanma: {total} so‘m"
        )
        return

    if admin and text == "🔑 API":
        rows = q("""
          SELECT id,name,base_url,api_key_enc,markup_uzs
          FROM api_credentials WHERE bot_id=%s ORDER BY id DESC
        """, (str(bot_id),), fetch=True)
        if not rows:
            await update.message.reply_text(
                "🔑 API yo‘q.\n\n"
                "Admin API ni dastur bazasiga xavfsiz tarzda qo‘shishi kerak."
            )
            return
        out = []
        for r in rows:
            try:
                token = dec(r[3])
                shown = mask_secret(token)
            except Exception:
                shown = "••••••••"
            out.append(f"{r[0]} | {r[1]}\nURL: {r[2]}\nToken: {shown}\nUstama: {r[4]} so‘m")
        await update.message.reply_text("\n\n".join(out))
        return

    if admin and text == "⚙️ Sozlamalar":
        row = get_bot(bot_id)
        await update.message.reply_text(
            f"⚙️ Bot sozlamalari\n\n@{row[3]}\n"
            f"Holat: {row[5]}\nObuna: {row[6] or '-'}"
        )
        return

    if admin and text == "⬅️ Menyu":
        await update.message.reply_text("Menyu", reply_markup=admin_kb())
        return

    await update.message.reply_text(
        "Menyudan foydalaning.",
        reply_markup=admin_kb() if admin else user_kb()
    )

# ============================================================
# RUNTIME MANAGER
# ============================================================
class Runtime:
    def __init__(self):
        self.apps = {}
        self.lock = asyncio.Lock()

    async def start_bot(self, bot_id):
        async with self.lock:
            if bot_id in self.apps:
                return
            row = get_bot(bot_id)
            if not row or not bot_is_active(bot_id):
                return
            token = dec(row[2])

            app = Application.builder().token(token).build()
            app.bot_data["bot_id"] = bot_id
            app.add_handler(CommandHandler("start", child_start))
            app.add_handler(CommandHandler("help", child_help))
            app.add_handler(CommandHandler("balance", child_balance))
            app.add_handler(CommandHandler("profile", child_profile))
            app.add_handler(CommandHandler("orders", child_orders))
            app.add_handler(CommandHandler("admin", child_admin))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, child_text))

            await app.initialize()
            await app.start()
            await app.bot.set_webhook(
                f"{RENDER_EXTERNAL_URL}/webhook/{bot_id}",
                drop_pending_updates=True
            )
            self.apps[bot_id] = app
            log.info("Child bot started: %s", row[3])

    async def stop_bot(self, bot_id):
        async with self.lock:
            app = self.apps.pop(bot_id, None)
            if not app:
                return
            try:
                await app.bot.delete_webhook(drop_pending_updates=True)
                await app.stop()
                await app.shutdown()
            except Exception as e:
                safe_error("Child bot stop xatosi", e)

RUNTIME = Runtime()
MAIN_LOOP = None

async def enqueue_update(app, update):
    await app.update_queue.put(update)

async def load_bots():
    rows = q("""
      SELECT id::text FROM bot_instances
      WHERE status='active'
        AND subscription_until IS NOT NULL
        AND subscription_until > NOW()
    """, fetch=True)
    for (bid,) in rows:
        try:
            await RUNTIME.start_bot(bid)
        except Exception as e:
            safe_error("Child bot startup xatosi", e)

async def maintenance():
    while True:
        try:
            rows = q("""
              SELECT id::text FROM bot_instances
              WHERE status='active' AND subscription_until IS NOT NULL
                    AND subscription_until <= NOW()
            """, fetch=True)
            for (bid,) in rows:
                q("""
                  UPDATE bot_instances
                  SET status='suspended',
                      suspended_at=NOW(),
                      retention_until=NOW()+INTERVAL '7 days'
                  WHERE id=%s
                """, (bid,), commit=True)
                await RUNTIME.stop_bot(bid)

            # Delete only expired runtime/data retention records after 7 days.
            q("""
              DELETE FROM bot_instances
              WHERE status='suspended'
                AND retention_until IS NOT NULL
                AND retention_until <= NOW()
            """, commit=True)
        except Exception as e:
            safe_error("Maintenance xatosi", e)
        await asyncio.sleep(3600)

# ============================================================
# FASTAPI WEBHOOK SERVER
# ============================================================
api = FastAPI()

@api.get("/", response_class=PlainTextResponse)
async def health():
    return "MULTI BOT PLATFORM OK"

@api.post("/webhook/platform")
async def platform_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, PLATFORM_APP.bot)
    if MAIN_LOOP is None:
        raise HTTPException(status_code=503, detail="runtime not ready")
    fut = asyncio.run_coroutine_threadsafe(enqueue_update(PLATFORM_APP, update), MAIN_LOOP)
    await asyncio.wrap_future(fut)
    return {"ok": True}

@api.post("/webhook/{bot_id}")
async def child_webhook(bot_id: str, request: Request):
    if bot_id not in RUNTIME.apps:
        raise HTTPException(status_code=404, detail="bot not active")
    data = await request.json()
    app = RUNTIME.apps[bot_id]
    update = Update.de_json(data, app.bot)
    if MAIN_LOOP is None:
        raise HTTPException(status_code=503, detail="runtime not ready")
    fut = asyncio.run_coroutine_threadsafe(enqueue_update(app, update), MAIN_LOOP)
    await asyncio.wrap_future(fut)
    return {"ok": True}

# ============================================================
# PLATFORM APP
# ============================================================
PLATFORM_APP = Application.builder().token(BOT_TOKEN).build()
PLATFORM_APP.add_handler(CommandHandler("start", platform_start))
PLATFORM_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, platform_text))

async def configure_platform():
    await PLATFORM_APP.bot.set_webhook(
        f"{RENDER_EXTERNAL_URL}/webhook/platform",
        drop_pending_updates=True
    )
    await PLATFORM_APP.bot.set_my_commands([
        BotCommand("start", "Boshlash")
    ])

async def async_main():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    init_db()
    await PLATFORM_APP.initialize()
    await PLATFORM_APP.start()
    await configure_platform()
    await load_bots()
    asyncio.create_task(maintenance())
    log.info("Platform started")

def run_async():
    asyncio.run(async_main())

if __name__ == "__main__":
    init_db()
    # Start asyncio runtime in main thread and HTTP server in another thread.
    t = threading.Thread(
        target=lambda: uvicorn.run(api, host="0.0.0.0", port=PORT, log_level="info"),
        daemon=True
    )
    t.start()
    run_async()

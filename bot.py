import sqlite3
import logging
import uuid
import json
import os
import asyncio
import threading
import contextvars
import hashlib
import base64
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:
    Fernet = None
    InvalidToken = Exception

from http.server import BaseHTTPRequestHandler, HTTPServer

from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# SOZLAMALAR
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except (ValueError, TypeError):
    ADMIN_ID = 0

PLAYPAY_API_KEY = os.getenv("PLAYPAY_API_KEY", "").strip()

PLAYPAY_BASE = "https://playpay.uz/api/v1"

# PlayPay Game ID
PUBG_GAME_ID = 141
MOBILE_LEGENDS_GAME_ID = 54

# 0 = PlayPay API narxining o'zi
DEFAULT_MARKUP = Decimal("0")

# Balans to'ldirish kartasi
PAYMENT_CARD = os.getenv("PAYMENT_CARD", "").strip()

# ===================== QO'SHIMCHA PROVIDERLAR =====================
# PayStars: Telegram Stars / Premium
PAYSTARS_API_KEY = os.getenv("PAYSTARS_API_KEY", "").strip()
PAYSTARS_API = os.getenv("PAYSTARS_API", "https://paystars.uz/api/v1").rstrip("/")
PAYSTARS_MARKUP_PERCENT = Decimal(os.getenv("PAYSTARS_MARKUP_PERCENT", "4.5"))

# AktivSim / Donuz: virtual raqamlar
AKTIVSIM_API_KEY = os.getenv("AKTIVSIM_API_KEY", "").strip() or os.getenv("DONUZ_API_KEY", "").strip()
AKTIVSIM_BASE = os.getenv(
    "AKTIVSIM_BASE",
    "https://ws2524.wineclo.com/AktivSimBot/api/v2/"
)
AKTIVSIM_MARKUP_PERCENT = Decimal(os.getenv("AKTIVSIM_MARKUP_PERCENT", "35"))

# SQLite
DB = "bot.db"
MAIN_DB = DB
CURRENT_DB = contextvars.ContextVar("current_db", default=MAIN_DB)
CHILD_APPS = {}
CHILD_TASKS = {}
MAIN_BOT_ID = 0
MAIN_BOT_USERNAME = ""
CHILD_DIR = Path("data/child_bots")
CHILD_DIR.mkdir(parents=True, exist_ok=True)


def active_db():
    return CURRENT_DB.get() or MAIN_DB


def set_active_db_for_bot(bot_id):
    if int(bot_id or 0) == int(MAIN_BOT_ID or 0):
        CURRENT_DB.set(MAIN_DB)
        return MAIN_DB
    c = sqlite3.connect(MAIN_DB, timeout=30)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT db_path FROM child_bots WHERE bot_id=?", (int(bot_id),)).fetchone()
    c.close()
    path = row["db_path"] if row and row["db_path"] else MAIN_DB
    CURRENT_DB.set(path)
    return path


def main_conn():
    c = sqlite3.connect(MAIN_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

# Render Web Service porti
try:
    PORT = int(os.getenv("PORT", "10000"))
except (ValueError, TypeError):
    PORT = 10000


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger(__name__)


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"PLAYPAY DONAT BOT OK"
        )

    def do_HEAD(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

    def log_message(self, format, *args):

        return


def start_health_server():

    try:

        server = HTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler
        )

        log.info(
            "Render HTTP server ishga tushdi: 0.0.0.0:%s",
            PORT
        )

        server.serve_forever()

    except Exception:

        log.exception(
            "HTTP health server xatosi"
        )


# ============================================================
# DATABASE
# ============================================================

def conn():

    c = sqlite3.connect(
        active_db(),
        timeout=30
    )

    c.row_factory = sqlite3.Row

    c.execute(
        "PRAGMA journal_mode=WAL"
    )

    c.execute(
        "PRAGMA foreign_keys=ON"
    )

    return c


def init_db():

    c = conn()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        balance REAL DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        requested_amount REAL,
        approved_amount REAL DEFAULT 0,
        photo_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        approved_at TEXT
    );

    CREATE TABLE IF NOT EXISTS games(
        game_id INTEGER PRIMARY KEY,
        name TEXT,
        id_label TEXT DEFAULT 'Player ID',
        requires_server INTEGER DEFAULT 0,
        amount_based INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS products(
        game_id INTEGER,
        paket_id INTEGER,
        game_name TEXT,
        package_name TEXT,
        price_usd REAL DEFAULT 0,
        api_price_uzs REAL DEFAULT 0,
        sale_price REAL DEFAULT 0,
        active INTEGER DEFAULT 1,
        updated_at TEXT,
        PRIMARY KEY(game_id, paket_id)
    );

    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        playpay_order_id TEXT,
        game_id INTEGER,
        paket_id INTEGER,
        product_name TEXT,
        player_id TEXT,
        fields_json TEXT,
        cost_usd REAL DEFAULT 0,
        charged_usd REAL DEFAULT 0,
        sale_price REAL DEFAULT 0,
        status TEXT,
        created_at TEXT,
        updated_at TEXT,
        notified TEXT DEFAULT '0'
    );

    CREATE TABLE IF NOT EXISTS promo_codes(
        code TEXT PRIMARY KEY,
        percent REAL,
        max_uses INTEGER DEFAULT 0,
        used INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS promo_users(
        user_id INTEGER,
        code TEXT,
        PRIMARY KEY(user_id, code)
    );

    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS balance_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        note TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS child_bots(
        bot_id INTEGER PRIMARY KEY,
        bot_username TEXT DEFAULT '',
        bot_name TEXT DEFAULT '',
        owner_user_id INTEGER NOT NULL,
        owner_username TEXT DEFAULT '',
        token_enc TEXT NOT NULL,
        db_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        trial_until TEXT NOT NULL,
        subscription_until TEXT DEFAULT '',
        grace_until TEXT NOT NULL,
        status TEXT DEFAULT 'trial',
        markup_uzs REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS subscription_plans(
        days INTEGER PRIMARY KEY,
        price_uzs REAL NOT NULL,
        active INTEGER DEFAULT 1
    );
    """)

    c.commit()
    c.close()

    set_default(
        "payment_card",
        PAYMENT_CARD
    )

    if active_db() == MAIN_DB:
        c = conn()
        plans = [
            (7, float(os.getenv("SUB_PRICE_7", "15000"))),
            (30, float(os.getenv("SUB_PRICE_30", "30000"))),
            (90, float(os.getenv("SUB_PRICE_90", "75000"))),
            (365, float(os.getenv("SUB_PRICE_365", "250000"))),
        ]
        for days, price in plans:
            c.execute("INSERT OR IGNORE INTO subscription_plans(days,price_uzs,active) VALUES (?,?,1)", (days, price))
        c.commit()
        c.close()


def set_default(key, value):

    c = conn()

    c.execute(
        """
        INSERT OR IGNORE INTO settings
        (key,value)
        VALUES (?,?)
        """,
        (
            key,
            str(value)
        )
    )

    c.commit()
    c.close()


def get_setting(key, default=""):

    c = conn()

    r = c.execute(
        """
        SELECT value
        FROM settings
        WHERE key=?
        """,
        (key,)
    ).fetchone()

    c.close()

    return r["value"] if r else default


def set_setting(key, value):

    c = conn()

    c.execute(
        """
        INSERT OR REPLACE INTO settings
        (key,value)
        VALUES (?,?)
        """,
        (
            key,
            str(value)
        )
    )

    c.commit()
    c.close()


# ============================================================
# QO'SHIMCHA PROVIDERLAR UCHUN DB USTUNLARI
# ============================================================
def ensure_external_schema():
    c = conn()
    try:
        existing = {
            r["name"]
            for r in c.execute("PRAGMA table_info(orders)").fetchall()
        }
        additions = {
            "provider": "TEXT DEFAULT 'playpay'",
            "service_type": "TEXT DEFAULT ''",
            "target": "TEXT DEFAULT ''",
            "quantity": "REAL DEFAULT 0",
            "months": "INTEGER DEFAULT 0",
            "provider_order_id": "TEXT DEFAULT ''",
        }
        for name, typ in additions.items():
            if name not in existing:
                c.execute(f"ALTER TABLE orders ADD COLUMN {name} {typ}")

        c.execute("""
            CREATE TABLE IF NOT EXISTS external_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                provider TEXT,
                service_type TEXT,
                provider_order_id TEXT,
                target TEXT,
                quantity REAL DEFAULT 0,
                months INTEGER DEFAULT 0,
                price REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        c.commit()
    finally:
        c.close()


# ============================================================
# BOT PLATFORM / SUBSCRIPTION
# ============================================================

def token_cipher():
    if Fernet is None:
        raise RuntimeError("cryptography o'rnatilmagan. requirements.txt ga cryptography qo'shing.")
    raw = os.getenv("BOT_TOKEN_ENCRYPTION_KEY", "").strip()
    if not raw:
        raw = base64.urlsafe_b64encode(hashlib.sha256(BOT_TOKEN.encode()).digest()).decode()
    try:
        return Fernet(raw.encode())
    except Exception:
        raise RuntimeError("BOT_TOKEN_ENCRYPTION_KEY noto'g'ri Fernet key.")


def encrypt_token(token):
    return token_cipher().encrypt(token.encode()).decode()


def decrypt_token(value):
    return token_cipher().decrypt(value.encode()).decode()


def child_bot_row(bot_id):
    c = main_conn()
    r = c.execute("SELECT * FROM child_bots WHERE bot_id=?", (int(bot_id),)).fetchone()
    c.close()
    return r


def child_owner_id(bot_id):
    r = child_bot_row(bot_id)
    return int(r["owner_user_id"]) if r else ADMIN_ID


def is_child_bot(context):
    return int(getattr(context.bot, "id", 0) or 0) != int(MAIN_BOT_ID or 0)


def bot_admin_id(context):
    return child_owner_id(context.bot.id) if is_child_bot(context) else ADMIN_ID


def set_request_db(context):
    return set_active_db_for_bot(context.bot.id)


def child_active(row):
    if not row:
        return False
    now = datetime.now()
    trial = datetime.fromisoformat(row["trial_until"]) if row["trial_until"] else now
    sub = datetime.fromisoformat(row["subscription_until"]) if row["subscription_until"] else None
    return now < trial or (sub and now < sub)


def child_grace_expired(row):
    if not row:
        return True
    return datetime.now() >= datetime.fromisoformat(row["grace_until"])


def child_status(row):
    if not row:
        return "deleted"
    now = datetime.now()
    trial = datetime.fromisoformat(row["trial_until"]) if row["trial_until"] else now
    sub = datetime.fromisoformat(row["subscription_until"]) if row["subscription_until"] else None
    if now < trial:
        return "trial"
    if sub and now < sub:
        return "active"
    return "expired"


def refresh_child_status(bot_id):
    r = child_bot_row(bot_id)
    if not r:
        return None
    status = child_status(r)
    c = main_conn()
    c.execute("UPDATE child_bots SET status=? WHERE bot_id=?", (status, int(bot_id)))
    c.commit(); c.close()
    return status


def child_price(base_price, markup):
    return max(0, Decimal(str(base_price or 0)) + Decimal(str(markup or 0))).quantize(Decimal("1"))


def get_child_markup(context):
    if not is_child_bot(context):
        return Decimal("0")
    r = child_bot_row(context.bot.id)
    return Decimal(str(r["markup_uzs"] or 0)) if r else Decimal("0")


def child_turnover(bot_id):
    r = child_bot_row(bot_id)
    if not r:
        return 0, 0
    db = r["db_path"]
    c = sqlite3.connect(db, timeout=30)
    row = c.execute("SELECT COUNT(*), COALESCE(SUM(sale_price),0) FROM orders WHERE status NOT IN ('failed','cancelled','rejected')").fetchone()
    c.close()
    return int(row[0] or 0), float(row[1] or 0)


def subscription_plans():
    c = main_conn()
    rows = c.execute("SELECT * FROM subscription_plans WHERE active=1 ORDER BY days").fetchall()
    c.close()
    return rows


def format_dt(value):
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


async def validate_bot_token(token):
    token = token.strip()
    if not token or len(token) < 20:
        raise RuntimeError("Bot token noto'g'ri.")
    r = await asyncio.to_thread(requests.get, f"https://api.telegram.org/bot{token}/getMe", timeout=20)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError("Telegram javobi noto'g'ri.")
    if not r.ok or not data.get("ok"):
        raise RuntimeError("Bot token ishlamaydi.")
    return data["result"]


async def create_child_bot(owner_id, owner_username, token):
    me = await validate_bot_token(token)
    bot_id = int(me["id"])
    if bot_id == MAIN_BOT_ID:
        raise RuntimeError("Bu asosiy bot tokeni.")
    old = child_bot_row(bot_id)
    if old:
        if int(old["owner_user_id"]) != int(owner_id):
            raise RuntimeError("Bu bot boshqa foydalanuvchiga tegishli.")
        # existing bot: update owner username and token
        token_enc = encrypt_token(token)
        c = main_conn()
        c.execute("UPDATE child_bots SET bot_username=?, bot_name=?, owner_username=?, token_enc=? WHERE bot_id=?", (me.get("username", ""), me.get("first_name", ""), owner_username or "", token_enc, bot_id))
        c.commit(); c.close()
        await start_child_bot(bot_id)
        return me, False

    now = datetime.now()
    trial = now + timedelta(days=1)
    grace = trial + timedelta(days=7)
    db_path = str(CHILD_DIR / f"{bot_id}.db")
    CURRENT_DB.set(db_path)
    init_db(); ensure_external_schema()
    CURRENT_DB.set(MAIN_DB)
    # copy current catalog/settings into child DB
    copy_catalog_to_child(db_path)
    c = main_conn()
    c.execute("""INSERT INTO child_bots(bot_id,bot_username,bot_name,owner_user_id,owner_username,token_enc,db_path,created_at,trial_until,subscription_until,grace_until,status,markup_uzs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""", (bot_id, me.get("username", ""), me.get("first_name", ""), int(owner_id), owner_username or "", encrypt_token(token), db_path, now.isoformat(), trial.isoformat(), "", grace.isoformat(), "trial"))
    c.commit(); c.close()
    await start_child_bot(bot_id)
    return me, True


def copy_catalog_to_child(db_path):
    src = main_conn()
    games_rows = src.execute("SELECT * FROM games").fetchall()
    prod_rows = src.execute("SELECT * FROM products").fetchall()
    settings_rows = src.execute("SELECT key,value FROM settings WHERE key IN ('payment_card','channel_id')").fetchall()
    src.close()
    c = sqlite3.connect(db_path, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    for r in games_rows:
        c.execute("INSERT OR REPLACE INTO games(game_id,name,id_label,requires_server,amount_based,active,updated_at) VALUES(?,?,?,?,?,?,?)", tuple(r[x] for x in ("game_id","name","id_label","requires_server","amount_based","active","updated_at")))
    for r in prod_rows:
        c.execute("INSERT OR REPLACE INTO products(game_id,paket_id,game_name,package_name,price_usd,api_price_uzs,sale_price,active,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", tuple(r[x] for x in ("game_id","paket_id","game_name","package_name","price_usd","api_price_uzs","sale_price","active","updated_at")))
    for r in settings_rows:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (r["key"], r["value"]))
    c.commit(); c.close()


def propagate_product_price(game_id, paket_id, price):
    c = main_conn()
    rows = c.execute("SELECT db_path FROM child_bots").fetchall()
    c.close()
    for r in rows:
        try:
            db = r["db_path"]
            cc = sqlite3.connect(db, timeout=30)
            cc.execute("UPDATE products SET sale_price=?,updated_at=? WHERE game_id=? AND paket_id=?", (float(price), datetime.now().isoformat(), game_id, paket_id))
            cc.commit(); cc.close()
        except Exception:
            log.exception("Child katalog narxini yangilash xatosi")


def sync_all_child_catalogs():
    c = main_conn(); rows = c.execute("SELECT db_path FROM child_bots").fetchall(); c.close()
    for r in rows:
        try: copy_catalog_to_child(r["db_path"])
        except Exception: log.exception("Child katalog sync xatosi")


async def start_child_bot(bot_id):
    if bot_id in CHILD_APPS:
        return
    row = child_bot_row(bot_id)
    if not row or not child_active(row):
        return
    token = decrypt_token(row["token_enc"])
    app = build_application(token, child=True)
    CHILD_APPS[bot_id] = app
    await app.initialize()
    await app.start()
    if app.updater:
        await app.updater.start_polling(drop_pending_updates=True)
    log.info("Child bot ishga tushdi: @%s (%s)", row["bot_username"], bot_id)


async def stop_child_bot(bot_id):
    app = CHILD_APPS.pop(bot_id, None)
    if not app:
        return
    try:
        if app.updater:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("Child bot to'xtatishda xato")


async def manage_child_bots(context):
    set_request_db(context)
    c = main_conn(); rows = c.execute("SELECT * FROM child_bots").fetchall(); c.close()
    for row in rows:
        status = child_status(row)
        if status == "expired" and child_grace_expired(row):
            await stop_child_bot(int(row["bot_id"]))
            try:
                Path(row["db_path"]).unlink(missing_ok=True)
            except Exception:
                pass
            c = main_conn(); c.execute("DELETE FROM child_bots WHERE bot_id=?", (row["bot_id"],)); c.commit(); c.close()
            continue
        c = main_conn(); c.execute("UPDATE child_bots SET status=? WHERE bot_id=?", (status, row["bot_id"])); c.commit(); c.close()
        if status in ("trial", "active") and int(row["bot_id"]) not in CHILD_APPS:
            try: await start_child_bot(int(row["bot_id"]))
            except Exception: log.exception("Child bot start xatosi")
        elif status == "expired" and int(row["bot_id"]) in CHILD_APPS:
            await stop_child_bot(int(row["bot_id"]))


def build_application(token, child=False):
    app = Application.builder().token(token).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.ANIMATION | filters.VIDEO, media_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    if app.job_queue:
        app.job_queue.run_repeating(check_orders, interval=30, first=30)
        if not child:
            app.job_queue.run_repeating(manage_child_bots, interval=60, first=5)
    return app


async def child_platform_access(update, context):
    if not is_child_bot(context):
        return True
    row = child_bot_row(context.bot.id)
    if not row:
        return False
    status = child_status(row)
    if status in ("trial", "active"):
        return True
    await update.effective_message.reply_text(
        "⛔ Obuna faol emas.\n\n"
        "Bot egasi asosiy botga kirib obuna sotib olishi kerak.\n"
        f"📅 Saqlash muddati: {format_dt(row['grace_until'])} gacha."
    )
    return False


async def subscription_menu(update, context):
    q = update.callback_query
    rows = subscription_plans()
    text = "💳 <b>Bot obunasi</b>\n\n1 kunlik sinov muddati bepul.\n\n"
    kb=[]
    for r in rows:
        text += f"📅 {r['days']} kun — {r['price_uzs']:,.0f} so'm\n"
        kb.append([InlineKeyboardButton(f"{r['days']} kun — {r['price_uzs']:,.0f} so'm", callback_data=f"sub_buy_{r['days']}")])
    kb.append([InlineKeyboardButton("🔙 Orqaga", callback_data="back_home")])
    await q.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


async def buy_subscription(update, context, days):
    q=update.callback_query
    if is_child_bot(context):
        await q.message.reply_text("Obunani asosiy platforma botidan sotib oling.")
        return
    c=main_conn(); plan=c.execute("SELECT * FROM subscription_plans WHERE days=? AND active=1",(days,)).fetchone(); c.close()
    if not plan:
        return await q.message.reply_text("❌ Obuna paketi topilmadi.")
    price=Decimal(str(plan["price_uzs"]))
    uid=q.from_user.id
    if get_balance(uid)<price:
        return await q.message.reply_text(f"❌ Balans yetarli emas.\nKerak: {price:,.0f} so'm\nBalans: {get_balance(uid):,.0f} so'm")
    c=main_conn(); bots=c.execute("SELECT * FROM child_bots WHERE owner_user_id=? ORDER BY created_at DESC",(uid,)).fetchall(); c.close()
    if not bots:
        return await q.message.reply_text("Avval 🤖 Bot qo'shing.")
    if len(bots)>1:
        context.user_data["subscription_days"]=days
        kb=[[InlineKeyboardButton(f"@{b['bot_username'] or b['bot_id']}",callback_data=f"sub_choose_{b['bot_id']}")] for b in bots]
        kb.append([InlineKeyboardButton("❌ Bekor qilish",callback_data="cancel")])
        return await q.message.reply_text("Obuna qaysi bot uchun?",reply_markup=InlineKeyboardMarkup(kb))
    await activate_subscription(bots[0]["bot_id"], uid, days, price, context)


async def activate_subscription(bot_id, uid, days, price, context):
    c=main_conn(); row=c.execute("SELECT * FROM child_bots WHERE bot_id=? AND owner_user_id=?",(bot_id,uid)).fetchone(); c.close()
    if not row:
        return await context.bot.send_message(uid,"❌ Bot topilmadi.")
    now=datetime.now()
    current=datetime.fromisoformat(row["subscription_until"]) if row["subscription_until"] else now
    start=max(now,current)
    until=start+timedelta(days=days)
    grace=until+timedelta(days=7)
    add_balance(uid,-price,"subscription",f"Bot obunasi {days} kun")
    c=main_conn(); c.execute("UPDATE child_bots SET subscription_until=?,grace_until=?,status=? WHERE bot_id=?",(until.isoformat(),grace.isoformat(),"active",bot_id)); c.commit(); c.close()
    await start_child_bot(int(bot_id))
    await context.bot.send_message(uid,f"✅ Obuna faollashtirildi!\n\n🤖 @{row['bot_username'] or bot_id}\n📅 {days} kun\n⏰ Tugaydi: {format_dt(until.isoformat())}")


async def choose_subscription_bot(update, context, bot_id):
    days=int(context.user_data.get("subscription_days",0))
    c=main_conn(); plan=c.execute("SELECT price_uzs FROM subscription_plans WHERE days=?",(days,)).fetchone(); c.close()
    if not plan:
        return
    await activate_subscription(bot_id, update.effective_user.id, days, Decimal(str(plan["price_uzs"])), context)
    context.user_data.clear()


async def add_bot_start(update, context):
    q=update.callback_query
    context.user_data["state"]="add_bot_token"
    await q.message.reply_text("🤖 Yangi bot qo'shish\n\nBotFather bergan tokenni yuboring.\n\n🔐 Token boshqa foydalanuvchilarga ko'rsatilmaydi.")


async def bot_list(update, context):
    q=update.callback_query
    c=main_conn(); rows=c.execute("SELECT * FROM child_bots WHERE owner_user_id=? ORDER BY created_at DESC",(q.from_user.id,)).fetchall(); c.close()
    if not rows:
        return await q.message.reply_text("🤖 Sizda hali qo'shilgan bot yo'q.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Bot qo'shish",callback_data="bot_add")]]))
    kb=[[InlineKeyboardButton(f"@{r['bot_username'] or r['bot_id']} — {child_status(r)}",callback_data=f"bot_manage_{r['bot_id']}")] for r in rows]
    await q.message.reply_text("🤖 Botlaringiz:",reply_markup=InlineKeyboardMarkup(kb))


async def bot_manage(update, context, bot_id):
    q=update.callback_query; r=child_bot_row(bot_id)
    if not r or int(r["owner_user_id"])!=q.from_user.id: return await q.message.reply_text("❌ Bu bot sizniki emas.")
    orders,turn=child_turnover(bot_id); st=child_status(r)
    until=r["subscription_until"] or r["trial_until"]
    text=(f"🤖 <b>@{r['bot_username'] or bot_id}</b>\n\n🆔 Bot ID: <code>{bot_id}</code>\n👤 Egasi ID: <code>{r['owner_user_id']}</code>\n📅 Qo'shilgan: {format_dt(r['created_at'])}\n💰 Aylanma: {turn:,.0f} so'm\n📦 Buyurtmalar: {orders}\n🟢 Holati: {st}\n⏰ Muddati: {format_dt(until)}\n💵 Ustama: {r['markup_uzs']:,.0f} so'm")
    kb=[[InlineKeyboardButton("⚙️ Botlar sozlamalari",callback_data=f"bot_settings_{bot_id}")],[InlineKeyboardButton("▶️ Ishga tushirish",callback_data=f"bot_start_{bot_id}"),InlineKeyboardButton("⛔ To'xtatish",callback_data=f"bot_stop_{bot_id}")],[InlineKeyboardButton("🔙 Orqaga",callback_data="bot_list")]]
    await q.message.reply_text(text,parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))


async def bot_settings(update, context, bot_id):
    q=update.callback_query; r=child_bot_row(bot_id)
    if not r or int(r["owner_user_id"])!=q.from_user.id: return await q.message.reply_text("❌ Bu bot sizniki emas.")
    context.user_data["settings_bot_id"]=bot_id
    kb=[[InlineKeyboardButton("💰 Ustama UZS",callback_data=f"bot_markup_{bot_id}")],[InlineKeyboardButton("▶️ Botni ishga tushirish",callback_data=f"bot_start_{bot_id}")],[InlineKeyboardButton("⛔ Botni to'xtatish",callback_data=f"bot_stop_{bot_id}")],[InlineKeyboardButton("🔙 Orqaga",callback_data=f"bot_manage_{bot_id}")]]
    await q.message.reply_text(f"⚙️ @{r['bot_username'] or bot_id} sozlamalari\n\n💰 Hozirgi ustama: {r['markup_uzs']:,.0f} so'm",reply_markup=InlineKeyboardMarkup(kb))


async def bot_markup_start(update, context, bot_id):
    q=update.callback_query; r=child_bot_row(bot_id)
    if not r or int(r["owner_user_id"])!=q.from_user.id: return
    context.user_data["state"]="bot_markup"; context.user_data["settings_bot_id"]=bot_id
    await q.message.reply_text("💰 Ustama miqdorini UZSda yuboring.\nMasalan: 3000")


async def bot_start_manual(update, context, bot_id):
    q=update.callback_query; r=child_bot_row(bot_id)
    if not r or int(r["owner_user_id"])!=q.from_user.id: return
    if not child_active(r): return await q.message.reply_text("⛔ Obuna faol emas.")
    await start_child_bot(int(bot_id)); await q.message.reply_text("▶️ Bot ishga tushirildi.")


async def bot_stop_manual(update, context, bot_id):
    q=update.callback_query; r=child_bot_row(bot_id)
    if not r or int(r["owner_user_id"])!=q.from_user.id: return
    await stop_child_bot(int(bot_id)); await q.message.reply_text("⛔ Bot to'xtatildi.")


async def admin_bots(update, context):
    q=update.callback_query
    if q.from_user.id != ADMIN_ID or is_child_bot(context): return
    c=main_conn(); rows=c.execute("SELECT * FROM child_bots ORDER BY created_at DESC LIMIT 100").fetchall(); c.close()
    if not rows: return await q.message.reply_text("🤖 Hali mijoz botlari yo'q.")
    kb=[[InlineKeyboardButton(f"@{r['bot_username'] or r['bot_id']} | {child_status(r)}",callback_data=f"adm_bot_{r['bot_id']}")] for r in rows]
    await q.message.reply_text("🤖 Barcha mijoz botlari:",reply_markup=InlineKeyboardMarkup(kb))


async def admin_bot_detail(update, context, bot_id):
    q=update.callback_query
    if q.from_user.id != ADMIN_ID or is_child_bot(context): return
    r=child_bot_row(bot_id)
    if not r: return await q.message.reply_text("❌ Bot topilmadi.")
    orders,turn=child_turnover(bot_id)
    text=(f"🤖 <b>@{r['bot_username'] or bot_id}</b>\n\n🆔 Bot ID: <code>{bot_id}</code>\n👤 Egasi: @{r['owner_username'] or 'username'}\n👤 Owner ID: <code>{r['owner_user_id']}</code>\n📅 Qo'shilgan: {format_dt(r['created_at'])}\n💰 Aylanma: {turn:,.0f} so'm\n📦 Buyurtmalar: {orders}\n🟢 Holati: {child_status(r)}\n⏰ Trial: {format_dt(r['trial_until'])}\n💳 Obuna: {format_dt(r['subscription_until'])}\n🗑 Saqlash: {format_dt(r['grace_until'])}")
    kb=[[InlineKeyboardButton("▶️ Ishga tushirish",callback_data=f"adm_bot_start_{bot_id}"),InlineKeyboardButton("⛔ To'xtatish",callback_data=f"adm_bot_stop_{bot_id}")],[InlineKeyboardButton("🔙 Orqaga",callback_data="adm_bots")]]
    await q.message.reply_text(text,parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))


# ============================================================
# PAYSTARS API
# ============================================================
def ps_headers(idempotency_key=None):
    h = {
        "X-API-Key": PAYSTARS_API_KEY,
        "Content-Type": "application/json",
    }
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


def ps_get(path):
    r = requests.get(
        PAYSTARS_API + path,
        headers=ps_headers(),
        timeout=25
    )
    try:
        data = r.json()
    except Exception:
        data = {"detail": r.text[:500]}
    if not r.ok:
        raise RuntimeError(f"PayStars HTTP {r.status_code}")
    return data


def ps_post(path, payload, key=None):
    r = requests.post(
        PAYSTARS_API + path,
        headers=ps_headers(key),
        json=payload,
        timeout=30
    )
    try:
        data = r.json()
    except Exception:
        data = {"detail": r.text[:500]}
    if not r.ok:
        raise RuntimeError(f"PayStars HTTP {r.status_code}")
    return data


def ps_account():
    return ps_get("/account")


def ps_check_user(username, kind):
    return ps_post(
        "/check-username",
        {"username": username, "kind": kind}
    )


def ps_buy_stars(username, quantity, token):
    return ps_post(
        "/stars/buy",
        {
            "username": username,
            "quantity": quantity,
            "verification_token": token,
        },
        "stars_" + str(uuid.uuid4()),
    )


def ps_buy_premium(username, months, token):
    return ps_post(
        "/premium/buy",
        {
            "username": username,
            "months": months,
            "verification_token": token,
        },
        "premium_" + str(uuid.uuid4()),
    )


def ps_sell(value):
    return round(
        float(value) * (1 + float(PAYSTARS_MARKUP_PERCENT) / 100),
        2
    )


def ps_pricing():
    return ps_account().get("pricing", {})


# ============================================================
# AKTIVSIM / DONUZ API
# ============================================================
def aktivsim_get(action, **params):
    if not AKTIVSIM_API_KEY:
        return {"ok": False, "error": "AKTIVSIM_API_KEY/DONUZ_API_KEY sozlanmagan"}

    params["action"] = action
    params["apikey"] = AKTIVSIM_API_KEY
    try:
        r = requests.get(
            AKTIVSIM_BASE,
            params=params,
            timeout=20
        )
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": "API JSON qaytarmadi"}
    except Exception:
        return {"ok": False, "error": "AktivSim API bilan aloqa xatosi"}


def aktivsim_countries():
    return aktivsim_get("getCountries")


def aktivsim_balance():
    return aktivsim_get("getBalance")


def aktivsim_buy(country_code):
    return aktivsim_get("buyNumber", country_code=country_code)


def aktivsim_code(order_id):
    return aktivsim_get("getCode", order_id=order_id)


def aktivsim_sale_price(api_price):
    return float(
        Decimal(str(api_price or 0)) *
        (Decimal("1") + AKTIVSIM_MARKUP_PERCENT / Decimal("100"))
    )


def save_external_order(
    user_id,
    provider,
    service_type,
    provider_order_id,
    target,
    quantity=0,
    months=0,
    price=0,
    status="pending",
):
    c = conn()
    now_s = datetime.now().isoformat()
    try:
        c.execute(
            """
            INSERT INTO external_orders
            (user_id,provider,service_type,provider_order_id,target,
             quantity,months,price,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id, provider, service_type, str(provider_order_id or ""),
                target or "", float(quantity or 0), int(months or 0),
                float(price or 0), status, now_s
            )
        )
        # Also mirror it into the main orders table so existing
        # admin/user order history can see all providers.
        c.execute(
            """
            INSERT INTO orders
            (user_id,product_name,player_id,sale_price,status,created_at,
             updated_at,provider,service_type,target,quantity,months,
             provider_order_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id,
                service_type,
                target or "",
                float(price or 0),
                status,
                now_s,
                now_s,
                provider,
                service_type,
                target or "",
                float(quantity or 0),
                int(months or 0),
                str(provider_order_id or ""),
            )
        )
        c.commit()
    finally:
        c.close()


# ============================================================
# PAYSTARS UI / FLOW
# ============================================================
def paystars_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ Telegram Stars", callback_data="ps_stars"),
        ],
        [
            InlineKeyboardButton("💎 Telegram Premium", callback_data="ps_premium"),
        ],
        [
            InlineKeyboardButton("🔙 Orqaga", callback_data="back_home"),
        ],
    ])


async def paystars_menu(update, context):
    q = update.callback_query
    try:
        p = await asyncio.to_thread(ps_pricing)
        star = ps_sell(p.get("star_price", 0))
        p3 = ps_sell(p.get("premium_3_price", 0))
        p6 = ps_sell(p.get("premium_6_price", 0))
        p12 = ps_sell(p.get("premium_12_price", 0))
        text = (
            "⭐ <b>Telegram xizmatlari</b>\n\n"
            f"⭐ 1 Stars: {star:,.0f} so'm\n"
            "⭐ Minimal: 50 Stars\n\n"
            f"💎 Premium 3 oy: {p3:,.0f} so'm\n"
            f"💎 Premium 6 oy: {p6:,.0f} so'm\n"
            f"💎 Premium 12 oy: {p12:,.0f} so'm"
        )
    except Exception:
        text = "⭐ Telegram Stars va 💎 Telegram Premium"
    await q.message.reply_text(text, parse_mode="HTML", reply_markup=paystars_kb())


async def ps_stars_start(update, context):
    q = update.callback_query
    context.user_data.clear()
    context.user_data["state"] = "ps_stars_username"
    await q.message.reply_text(
        "⭐ Stars\n\n@username yuboring:"
    )


async def ps_premium_start(update, context):
    q = update.callback_query
    context.user_data.clear()
    context.user_data["state"] = "ps_premium_month"
    await q.message.reply_text(
        "💎 Premium muddatini tanlang:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("3 oy", callback_data="ps_pm_3"),
                InlineKeyboardButton("6 oy", callback_data="ps_pm_6"),
            ],
            [InlineKeyboardButton("12 oy", callback_data="ps_pm_12")],
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")],
        ])
    )


async def ps_premium_month(update, context):
    q = update.callback_query
    months = int(q.data.rsplit("_", 1)[1])
    context.user_data["state"] = "ps_premium_username"
    context.user_data["ps_months"] = months
    await q.message.reply_text("👤 Premium kimga?\n\n@username yuboring:")


async def ps_confirm(update, context, kind):
    q = update.callback_query
    uid = q.from_user.id
    if kind == "stars":
        username = context.user_data.get("ps_username")
        quantity = int(context.user_data.get("ps_quantity", 0))
        months = 0
        token = context.user_data.get("ps_token")
        price = float(context.user_data.get("ps_price", 0))
        label = f"⭐ {quantity} Stars"
    else:
        username = context.user_data.get("ps_username")
        quantity = 0
        months = int(context.user_data.get("ps_months", 0))
        token = context.user_data.get("ps_token")
        price = float(context.user_data.get("ps_price", 0))
        label = f"💎 Premium {months} oy"

    if not username or not token or price <= 0:
        await q.message.reply_text("❌ Buyurtma ma'lumotlari eskirgan.")
        context.user_data.clear()
        return

    if get_balance(uid) < Decimal(str(price)):
        await q.message.reply_text(
            f"❌ Balans yetarli emas.\n"
            f"Kerak: {price:,.0f} so'm\n"
            f"Balans: {get_balance(uid):,.0f} so'm"
        )
        return

    add_balance(uid, -price, "purchase", f"PayStars {label}")
    try:
        result = await asyncio.to_thread(
            ps_buy_stars, username, quantity, token
        ) if kind == "stars" else await asyncio.to_thread(
            ps_buy_premium, username, months, token
        )
        oid = str(result.get("order_id", ""))
        status = str(result.get("status", "processing"))
        if not oid:
            raise RuntimeError("PayStars order_id qaytarmadi")

        save_external_order(
            uid, "paystars", kind, oid, username,
            quantity, months, price, status
        )
        await q.message.reply_text(
            f"✅ <b>Buyurtma qabul qilindi!</b>\n\n"
            f"🆔 <code>{oid}</code>\n"
            f"👤 @{username}\n"
            f"{label}\n"
            f"💰 {price:,.0f} so'm\n"
            f"📊 {status}",
            parse_mode="HTML"
        )
        context.user_data.clear()
    except Exception:
        add_balance(uid, price, "refund", f"PayStars {label} refund")
        await q.message.reply_text(
            "❌ Buyurtma yaratilmadi.\n\n💰 Balansingiz qaytarildi."
        )
        context.user_data.clear()


async def paystars_balance_admin(update, context):
    q = update.callback_query
    if q.from_user.id != bot_admin_id(context):
        return
    if not PAYSTARS_API_KEY:
        return await q.message.reply_text(
            "❌ PAYSTARS_API_KEY Environment Variable sozlanmagan."
        )
    try:
        data = await asyncio.to_thread(ps_account)
        await q.message.reply_text(str(data))
    except Exception:
        await q.message.reply_text("❌ PayStars balansini olishda xatolik.")


# ============================================================
# AKTIVSIM UI / FLOW
# ============================================================
async def aktivsim_countries_handler(update, context):
    q = update.callback_query
    if not AKTIVSIM_API_KEY:
        return await q.message.reply_text(
            "❌ AKTIVSIM_API_KEY yoki DONUZ_API_KEY sozlanmagan."
        )

    res = await asyncio.to_thread(aktivsim_countries)
    if not res.get("ok") or not res.get("result"):
        return await q.message.reply_text(
            "❌ AktivSim davlatlar ro'yxatini olishda xatolik."
        )

    c = conn()
    custom = {
        row["country_code"]: row["custom_price"]
        for row in c.execute(
            "SELECT country_code,custom_price FROM custom_prices"
        ).fetchall()
    }
    c.close()

    rows = []
    for country in res["result"][:50]:
        code = country.get("country_code")
        name = country.get("name", code)
        flag = country.get("flag", "")
        api_price = float(country.get("price", 0) or 0)
        final = float(custom.get(code, aktivsim_sale_price(api_price)))
        rows.append([
            InlineKeyboardButton(
                f"{flag} {name} — {final:,.0f} so'm",
                callback_data=f"as_country_{code}"
            )
        ])

    rows.append([
        InlineKeyboardButton("🔙 Orqaga", callback_data="back_home")
    ])
    await q.message.reply_text(
        "🌍 <b>Virtual raqam</b>\n\nDavlatni tanlang:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def aktivsim_country_handler(update, context):
    q = update.callback_query
    uid = q.from_user.id
    code = q.data[len("as_country_"):]
    res = await asyncio.to_thread(aktivsim_countries)
    if not res.get("ok") or not res.get("result"):
        return await q.message.reply_text("❌ AktivSim API xatosi.")

    country = next(
        (x for x in res["result"]
         if str(x.get("country_code")) == str(code)),
        None
    )
    if not country:
        return await q.message.reply_text("❌ Davlat topilmadi.")

    c = conn()
    r = c.execute(
        "SELECT custom_price FROM custom_prices WHERE country_code=?",
        (code,)
    ).fetchone()
    c.close()

    api_price = float(country.get("price", 0) or 0)
    price = float(r["custom_price"]) if r else aktivsim_sale_price(api_price)

    if get_balance(uid) < Decimal(str(price)):
        return await q.message.reply_text(
            f"❌ Balansingiz yetarli emas.\n"
            f"Kerak: {price:,.0f} so'm\n"
            f"Balans: {float(get_balance(uid)):,.0f} so'm"
        )

    await q.message.edit_text("⏳ Raqam olinmoqda, kuting...")
    bought = await asyncio.to_thread(aktivsim_buy, code)

    if not bought.get("ok") or not bought.get("result"):
        return await q.message.edit_text(
            "❌ Raqamni olishda xatolik.\nQaytadan urinib ko'ring."
        )

    result = bought["result"]
    provider_oid = result.get("order_id", "")
    phone = result.get("phone", "")
    api_real_price = result.get("price", api_price)

    # Agar provider qaytargan narx boshqacha bo'lsa, sotuv narxini
    # oldindan tanlangan katalog narxida saqlaymiz.
    add_balance(uid, -price, "purchase", f"AktivSim {code}")
    save_external_order(
        uid, "aktivsim", "virtual_number", provider_oid,
        phone, 1, 0, price, "sold"
    )

    code_result = await asyncio.to_thread(aktivsim_code, provider_oid)
    sms_code = ""
    if code_result.get("ok") and code_result.get("result"):
        sms_code = (
            code_result["result"].get("code")
            or code_result["result"].get("sms_code")
            or ""
        )

    text = (
        "✅ <b>Raqam muvaffaqiyatli olindi!</b>\n\n"
        f"🌍 {country.get('name', code)}\n"
        f"📞 <code>+{phone}</code>\n"
        f"💰 {price:,.0f} so'm\n"
        f"🆔 {provider_oid}\n"
    )
    if sms_code:
        text += f"🔑 Kod: <code>{sms_code}</code>\n"
    else:
        text += "⏳ SMS kodi hali kelmagan bo'lishi mumkin.\n"

    await q.message.edit_text(text, parse_mode="HTML")


async def aktivsim_balance_admin(update, context):
    q = update.callback_query
    if q.from_user.id != bot_admin_id(context):
        return
    if not AKTIVSIM_API_KEY:
        return await q.message.reply_text(
            "❌ AKTIVSIM_API_KEY yoki DONUZ_API_KEY sozlanmagan."
        )
    data = await asyncio.to_thread(aktivsim_balance)
    if data.get("ok"):
        await q.message.reply_text(
            f"🔒 AktivSim balansi: {data.get('balance', 'Nomaʼlum')}"
        )
    else:
        await q.message.reply_text("❌ AktivSim balansini olishda xatolik.")


# ============================================================
# USER
# ============================================================

def ensure_user(u):

    if not u:
        return

    c = conn()

    now = datetime.now().isoformat()

    c.execute(
        """
        INSERT OR IGNORE INTO users
        (user_id,username,first_name,created_at)
        VALUES (?,?,?,?)
        """,
        (
            u.id,
            u.username or "",
            u.first_name or "",
            now
        )
    )

    c.execute(
        """
        UPDATE users
        SET username=?,
            first_name=?
        WHERE user_id=?
        """,
        (
            u.username or "",
            u.first_name or "",
            u.id
        )
    )

    c.commit()
    c.close()


def user_exists(uid):

    c = conn()

    r = c.execute(
        """
        SELECT user_id
        FROM users
        WHERE user_id=?
        """,
        (uid,)
    ).fetchone()

    c.close()

    return r is not None


def get_balance(uid):

    c = conn()

    r = c.execute(
        """
        SELECT balance
        FROM users
        WHERE user_id=?
        """,
        (uid,)
    ).fetchone()

    c.close()

    return (
        Decimal(str(r["balance"]))
        if r
        else Decimal("0")
    )


def add_balance(
    uid,
    amount,
    tx_type="manual",
    note=""
):

    amount = Decimal(str(amount))

    c = conn()

    c.execute(
        """
        UPDATE users
        SET balance=balance+?
        WHERE user_id=?
        """,
        (
            float(amount),
            uid
        )
    )

    c.execute(
        """
        INSERT INTO balance_history
        (user_id,amount,type,note,created_at)
        VALUES (?,?,?,?,?)
        """,
        (
            uid,
            float(amount),
            tx_type,
            note,
            datetime.now().isoformat()
        )
    )

    c.commit()
    c.close()


# ============================================================
# PLAYPAY API
# ============================================================

def api_headers():

    return {
        "X-API-Key": PLAYPAY_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def api_get(path, params=None):

    try:

        r = requests.get(
            PLAYPAY_BASE + path,
            headers=api_headers(),
            params=params,
            timeout=30
        )

        try:

            data = r.json()

        except Exception:

            data = {
                "ok": False,
                "error": r.text
            }

        log.info(
            "PlayPay GET %s | status=%s | data=%s",
            path,
            r.status_code,
            data
        )

        return r.status_code, data

    except Exception as e:

        log.exception(
            "PlayPay GET xatosi"
        )

        return 0, {
            "ok": False,
            "error": str(e)
        }


def api_post(path, body):

    try:

        headers = {
            **api_headers(),
            "Idempotency-Key": str(
                uuid.uuid4()
            )
        }

        r = requests.post(
            PLAYPAY_BASE + path,
            headers=headers,
            json=body,
            timeout=30
        )

        try:

            data = r.json()

        except Exception:

            data = {
                "ok": False,
                "error": r.text
            }

        log.info(
            "PlayPay POST %s | status=%s | data=%s",
            path,
            r.status_code,
            data
        )

        return r.status_code, data

    except Exception as e:

        log.exception(
            "PlayPay POST xatosi"
        )

        return 0, {
            "ok": False,
            "error": str(e)
        }


def get_games_api():

    status, data = api_get(
        "/games"
    )

    if data.get("ok"):

        return data.get(
            "games",
            []
        )

    log.error(
        "PlayPay /games xatosi | status=%s | data=%s",
        status,
        data
    )

    return []


def get_packages_api(game_id):

    status, data = api_get(
        f"/games/{game_id}/packages",
        {
            "currency": "UZS"
        }
    )

    if data.get("ok"):

        return data

    log.error(
        "PlayPay packages xatosi | game_id=%s | status=%s | data=%s",
        game_id,
        status,
        data
    )

    return None


def get_playpay_order(order_id):

    return api_get(
        f"/order/{order_id}"
    )


def get_playpay_balance():

    return api_get(
        "/balance"
    )


# ============================================================
# NARX
# ============================================================

def calc_sale_price(api_uzs):

    try:

        price = Decimal(
            str(api_uzs)
        )

    except Exception:

        price = Decimal("0")

    sale = price * (
        Decimal("1")
        +
        DEFAULT_MARKUP / Decimal("100")
    )

    return sale.quantize(
        Decimal("1")
    )


# ============================================================
# GAME SAQLASH
# ============================================================

def save_game(game):

    c = conn()

    c.execute(
        """
        INSERT OR REPLACE INTO games
        (
            game_id,
            name,
            id_label,
            requires_server,
            amount_based,
            active,
            updated_at
        )
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            int(game["game_id"]),
            game.get(
                "name",
                str(game["game_id"])
            ),
            game.get(
                "id_label",
                "Player ID"
            ),
            1 if game.get(
                "requires_server"
            ) else 0,
            1 if game.get(
                "amount_based"
            ) else 0,
            1,
            datetime.now().isoformat()
        )
    )

    c.commit()
    c.close()


# ============================================================
# PACKAGE SAQLASH
# ============================================================

def save_package(
    game_id,
    game_name,
    package
):

    try:

        paket_id = int(
            package["paket_id"]
        )

    except Exception:

        return

    price = package.get(
        "price",
        {}
    ) or {}

    try:

        usd = Decimal(
            str(
                price.get(
                    "usd",
                    "0"
                )
            )
        )

    except Exception:

        usd = Decimal("0")

    try:

        uzs = Decimal(
            str(
                price.get(
                    "amount",
                    "0"
                )
            )
        )

    except Exception:

        uzs = Decimal("0")

    sale_price = calc_sale_price(
        uzs
    )

    c = conn()

    c.execute(
        """
        INSERT OR REPLACE INTO products
        (
            game_id,
            paket_id,
            game_name,
            package_name,
            price_usd,
            api_price_uzs,
            sale_price,
            active,
            updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            game_id,
            paket_id,
            game_name,
            package.get(
                "name",
                "Paket"
            ),
            float(usd),
            float(uzs),
            float(sale_price),
            1,
            datetime.now().isoformat()
        )
    )

    c.commit()
    c.close()

    log.info(
        "PACKAGE SAVED | game=%s | paket=%s | API=%s | SALE=%s",
        game_id,
        paket_id,
        uzs,
        sale_price
    )


# ============================================================
# PUBG 141
# ============================================================

def ensure_pubg():

    c = conn()

    row = c.execute(
        """
        SELECT game_id
        FROM games
        WHERE game_id=?
        """,
        (
            PUBG_GAME_ID,
        )
    ).fetchone()

    c.close()

    if not row:

        save_game({
            "game_id": PUBG_GAME_ID,
            "name": "PUBG Mobile",
            "id_label": "Player ID",
            "requires_server": False,
            "amount_based": False
        })


# ============================================================
# MOBILE LEGENDS 54
# ============================================================

def ensure_mobile_legends():

    c = conn()

    row = c.execute(
        """
        SELECT game_id
        FROM games
        WHERE game_id=?
        """,
        (
            MOBILE_LEGENDS_GAME_ID,
        )
    ).fetchone()

    c.close()

    if not row:

        save_game({
            "game_id": MOBILE_LEGENDS_GAME_ID,
            "name": "Mobile Legends",
            "id_label": "User ID",
            "requires_server": True,
            "amount_based": False
        })


# ============================================================
# CATALOG SYNC
# FAQAT ADMIN 🔄 KATALOG ORQALI ISHLAYDI
# ============================================================

def sync_catalog():

    game_count = 0
    package_count = 0
    synced_ids = set()

    games_api = get_games_api()

    if not games_api:

        ensure_pubg()
        ensure_mobile_legends()

        special_ok = False

        for gid in (
            PUBG_GAME_ID,
            MOBILE_LEGENDS_GAME_ID
        ):

            data = get_packages_api(
                gid
            )

            if not data:
                continue

            special_ok = True

            name = data.get(
                "game_name",
                (
                    "PUBG Mobile"
                    if gid == PUBG_GAME_ID
                    else "Mobile Legends"
                )
            )

            save_game({
                "game_id": gid,
                "name": name,
                "id_label": (
                    "Player ID"
                    if gid == PUBG_GAME_ID
                    else "User ID"
                ),
                "requires_server": (
                    gid == MOBILE_LEGENDS_GAME_ID
                ),
                "amount_based": False
            })

            synced_ids.add(gid)
            game_count += 1

            for package in data.get(
                "packages",
                []
            ):

                if not package.get(
                    "paket_id"
                ):
                    continue

                save_package(
                    gid,
                    name,
                    package
                )

                package_count += 1

        if not special_ok:

            return (
                False,
                "PlayPay katalogi olinmadi."
            )

        return (
            True,
            f"✅ {game_count} ta o'yin, "
            f"{package_count} ta paket yangilandi."
        )

    # Eski o'yinlarni yashiramiz
    c = conn()

    c.execute(
        "UPDATE games SET active=0"
    )

    c.execute(
        "UPDATE products SET active=0"
    )

    c.commit()
    c.close()

    # ========================================================
    # PLAYPAY O'YINLARI
    # ========================================================

    for game_data in games_api:

        try:

            gid = int(
                game_data["game_id"]
            )

        except Exception:

            continue

        try:

            save_game(
                game_data
            )

            game_count += 1
            synced_ids.add(gid)

            game_name = game_data.get(
                "name",
                str(gid)
            )

            data = get_packages_api(
                gid
            )

            if not data:
                continue

            for package in data.get(
                "packages",
                []
            ):

                if not package.get(
                    "paket_id"
                ):
                    continue

                save_package(
                    gid,
                    game_name,
                    package
                )

                package_count += 1

        except Exception:

            log.exception(
                "Katalog sync xatosi: game_id=%s",
                gid
            )

    # ========================================================
    # PUBG 141
    # ========================================================

    try:

        ensure_pubg()

        if PUBG_GAME_ID not in synced_ids:

            data = get_packages_api(
                PUBG_GAME_ID
            )

            if data:

                game_name = data.get(
                    "game_name",
                    "PUBG Mobile"
                )

                save_game({
                    "game_id": PUBG_GAME_ID,
                    "name": game_name,
                    "id_label": "Player ID",
                    "requires_server": False,
                    "amount_based": False
                })

                game_count += 1
                synced_ids.add(
                    PUBG_GAME_ID
                )

                for package in data.get(
                    "packages",
                    []
                ):

                    if not package.get(
                        "paket_id"
                    ):
                        continue

                    save_package(
                        PUBG_GAME_ID,
                        game_name,
                        package
                    )

                    package_count += 1

    except Exception:

        log.exception(
            "PUBG 141 sync xatosi"
        )

    # ========================================================
    # MOBILE LEGENDS 54
    # ========================================================

    try:

        ensure_mobile_legends()

        if MOBILE_LEGENDS_GAME_ID not in synced_ids:

            data = get_packages_api(
                MOBILE_LEGENDS_GAME_ID
            )

            if data:

                game_name = data.get(
                    "game_name",
                    "Mobile Legends"
                )

                save_game({
                    "game_id": MOBILE_LEGENDS_GAME_ID,
                    "name": game_name,
                    "id_label": "User ID",
                    "requires_server": True,
                    "amount_based": False
                })

                game_count += 1
                synced_ids.add(
                    MOBILE_LEGENDS_GAME_ID
                )

                for package in data.get(
                    "packages",
                    []
                ):

                    if not package.get(
                        "paket_id"
                    ):
                        continue

                    save_package(
                        MOBILE_LEGENDS_GAME_ID,
                        game_name,
                        package
                    )

                    package_count += 1

    except Exception:

        log.exception(
            "Mobile Legends 54 sync xatosi"
        )

    if game_count == 0:

        return (
            False,
            "PlayPay katalogi olinmadi."
        )

    return (
        True,
        f"✅ {game_count} ta o'yin, "
        f"{package_count} ta paket yangilandi."
    )


# ============================================================
# MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ O'yinlar / Donat", callback_data="games")],
        [InlineKeyboardButton("⭐ Stars / 💎 Premium", callback_data="paystars")],
        [InlineKeyboardButton("🇺🇿 Virtual raqam", callback_data="aktivsim_buy")],
        [InlineKeyboardButton("🤖 Bot qo'shish", callback_data="bot_add")],
        [InlineKeyboardButton("🤖 Botlarim", callback_data="bot_list")],
        [InlineKeyboardButton("⚙️ Botlar sozlamalari", callback_data="bot_list")],
        [InlineKeyboardButton("💳 Obuna sotib olish", callback_data="subscription")],
        [InlineKeyboardButton("💳 Balans to'ldirish", callback_data="deposit")],
        [InlineKeyboardButton("📦 Buyurtmalarim", callback_data="orders")],
        [
            InlineKeyboardButton("🎁 Promo kod", callback_data="promo"),
            InlineKeyboardButton("👤 Profil", callback_data="profile")
        ]
    ])


def admin_kb():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 Balans + / -",
                callback_data="adm_addbalance"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 To'lovlar",
                callback_data="adm_payments"
            ),
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="adm_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "🏆 Reyting",
                callback_data="adm_rating"
            ),
            InlineKeyboardButton(
                "👤 Foydalanuvchilar",
                callback_data="adm_users"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Buyurtmalar",
                callback_data="adm_orders"
            ),
            InlineKeyboardButton(
                "💵 Narxlar",
                callback_data="adm_prices"
            )
        ],
        [
            InlineKeyboardButton(
                "🎁 Promo",
                callback_data="adm_promo"
            ),
            InlineKeyboardButton(
                "💳 Karta",
                callback_data="adm_card"
            )
        ],
        [
            InlineKeyboardButton(
                "📢 Post",
                callback_data="adm_post"
            ),
            InlineKeyboardButton(
                "🔄 Katalog",
                callback_data="a_sync"
            )
        ],
        [
            InlineKeyboardButton(
                "🔐 PlayPay balansi",
                callback_data="adm_playpay_balance"
            )
        ],
        [
            InlineKeyboardButton("🤖 Bot boshqarish", callback_data="adm_bots"),
        ],
        [
            InlineKeyboardButton(
                "⭐ PayStars balansi",
                callback_data="adm_paystars_balance"
            ),
            InlineKeyboardButton(
                "🔒 AktivSim balansi",
                callback_data="adm_aktivsim_balance"
            ),
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):

    set_request_db(context)
    if not await child_platform_access(update, context):
        return
    ensure_user(
        update.effective_user
    )

    if not update.message:
        return

    await update.message.reply_text(
        "Assalomu Aleykum 👋\n\n"
        "🎮 Donat botiga xush kelibsiz!",
        reply_markup=main_menu()
    )


# ============================================================
# GAMES
# BU YERDA API SYNC YO'Q
# ============================================================

async def games(update, context):

    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        pass

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM games
        WHERE active=1
        ORDER BY
            CASE
                WHEN game_id=? THEN 0
                WHEN game_id=? THEN 1
                ELSE 2
            END,
            name
        """,
        (
            PUBG_GAME_ID,
            MOBILE_LEGENDS_GAME_ID
        )
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "❌ O'yinlar topilmadi.\n\n"
            "👑 Admin paneldan 🔄 Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    kb = []

    for r in rows:

        kb.append([
            InlineKeyboardButton(
                "🎮 " + r["name"],
                callback_data=f"g:{r['game_id']}"
            )
        ])

    await q.message.reply_text(
        "🎮 O'yinni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            kb[:100]
        )
    )


# ============================================================
# GAME PACKAGES
# BU YERDA API'DAN OLINMAYDI
# FAQAT DATABASE'DAGI KATALOG ISHLATILADI
# ============================================================

async def game(update, context):

    q = update.callback_query

    try:

        game_id = int(
            q.data.split(
                ":",
                1
            )[1]
        )

    except Exception:

        await q.message.reply_text(
            "❌ O'yin ID xato."
        )

        return

    try:
        await q.answer()
    except Exception:
        pass

    c = conn()

    g = c.execute(
        """
        SELECT *
        FROM games
        WHERE game_id=?
          AND active=1
        """,
        (game_id,)
    ).fetchone()

    rows = c.execute(
        """
        SELECT *
        FROM products
        WHERE game_id=?
          AND active=1
        ORDER BY paket_id
        """,
        (game_id,)
    ).fetchall()

    c.close()

    if not g:

        await q.message.reply_text(
            "❌ O'yin topilmadi.\n\n"
            "👑 Admin paneldan 🔄 Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    if not rows:

        await q.message.reply_text(
            f"❌ {g['name']} uchun paketlar topilmadi.\n\n"
            "👑 Admin paneldan 🔄 Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    game_name = g["name"]

    if game_id == PUBG_GAME_ID:

        id_label = "Player ID"
        requires_server = False

    elif game_id == MOBILE_LEGENDS_GAME_ID:

        id_label = "User ID"
        requires_server = True

    else:

        id_label = (
            g["id_label"]
            or "Player ID"
        )

        requires_server = bool(
            g["requires_server"]
        )

    context.user_data.update({
        "game_id": game_id,
        "game_name": game_name,
        "id_label": id_label,
        "requires_server": requires_server
    })

    kb = []

    for r in rows:

        try:

            sale = Decimal(
                str(r["sale_price"])
            )

        except Exception:

            continue

        kb.append([
            InlineKeyboardButton(
                f"{r['package_name']} — "
                f"{child_price(sale, get_child_markup(context)):,.0f} so'm",
                callback_data=(
                    f"o:{game_id}:{r['paket_id']}"
                )
            )
        ])

    if not kb:

        await q.message.reply_text(
            "❌ Paketlar topilmadi."
        )

        return

    await q.message.reply_text(
        f"📦 {game_name}\n\n"
        "Paketni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            kb[:100]
        )
    )


# ============================================================
# OFFER
# BU YERDA API FALLBACK YO'Q
# ============================================================

async def offer(update, context):

    q = update.callback_query

    try:

        _, gid, pid = q.data.split(
            ":",
            2
        )

        game_id = int(gid)
        paket_id = int(pid)

    except Exception:

        await q.message.reply_text(
            "❌ Paket ID xato."
        )

        return

    try:
        await q.answer()
    except Exception:
        pass

    c = conn()

    r = c.execute(
        """
        SELECT *
        FROM products
        WHERE game_id=?
          AND paket_id=?
          AND active=1
        """,
        (
            game_id,
            paket_id
        )
    ).fetchone()

    g = c.execute(
        """
        SELECT *
        FROM games
        WHERE game_id=?
          AND active=1
        """,
        (game_id,)
    ).fetchone()

    c.close()

    if game_id == MOBILE_LEGENDS_GAME_ID:

        id_label = "User ID"
        requires_server = True

    elif game_id == PUBG_GAME_ID:

        id_label = "Player ID"
        requires_server = False

    else:

        id_label = (
            g["id_label"]
            if g
            else "Player ID"
        )

        requires_server = (
            bool(g["requires_server"])
            if g
            else False
        )

    if not r:

        await q.message.reply_text(
            "❌ Paket katalogda topilmadi.\n\n"
            "👑 Admin paneldan 🔄 Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    context.user_data.update({

        "game_id": game_id,

        "paket_id": paket_id,

        "offer_name": r[
            "package_name"
        ],

        "price": child_price(r["sale_price"], get_child_markup(context)),

        "id_label": id_label,

        "requires_server": requires_server,

        "state": "player_id"
    })

    await q.message.reply_text(
        f"🎮 {r['game_name']}\n"
        f"📦 {r['package_name']}\n"
        f"💰 Narx: "
        f"{child_price(r['sale_price'], get_child_markup(context)):,.0f} so'm\n\n"
        f"🆔 {id_label} ni yuboring:\n\n"
        "Bekor qilish uchun /cancel"
    )


# ============================================================
# CONFIRM ORDER
# ============================================================

async def confirm_order(
    message,
    context
):

    price = Decimal(
        str(
            context.user_data.get(
                "price",
                0
            )
        )
    )

    promo = context.user_data.get(
        "promo_code"
    )

    discount = Decimal("0")

    if promo:

        c = conn()

        r = c.execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code=?
              AND active=1
            """,
            (promo,)
        ).fetchone()

        c.close()

        if r and (
            r["max_uses"] == 0
            or r["used"] < r["max_uses"]
        ):

            discount = (
                price
                *
                Decimal(
                    str(r["percent"])
                )
                /
                Decimal("100")
            )

    final_price = max(
        Decimal("0"),
        price - discount
    )

    context.user_data[
        "final_price"
    ] = final_price

    player_id = str(
        context.user_data.get(
            "player_id",
            ""
        )
    ).strip()

    server_id = str(
        context.user_data.get(
            "server_id",
            ""
        )
    ).strip()

    id_label = context.user_data.get(
        "id_label",
        "Player ID"
    )

    extra = ""

    if context.user_data.get(
        "requires_server",
        False
    ):

        extra = (
            f"🌐 Server ID: {server_id}\n"
        )

    await message.reply_text(
        f"📦 {context.user_data.get('offer_name','Paket')}\n\n"
        f"🆔 {id_label}: {player_id}\n"
        f"{extra}"
        f"💰 Narx: {final_price:,.0f} so'm\n"
        +
        (
            f"🎁 Chegirma: "
            f"{discount:,.0f} so'm\n"
            if discount
            else ""
        )
        +
        "\nBuyurtmani tasdiqlaysizmi?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Tasdiqlash",
                    callback_data="confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Bekor qilish",
                    callback_data="cancel"
                )
            ]
        ])
    )


# ============================================================
# CONFIRM -> PLAYPAY
# ============================================================

async def confirm(update, context):

    q = update.callback_query

    uid = q.from_user.id

    try:
        await q.answer()
    except Exception:
        pass

    try:

        price = Decimal(
            str(
                context.user_data.get(
                    "final_price",
                    0
                )
            )
        )

    except Exception:

        price = Decimal("0")

    if price <= 0:

        await q.message.reply_text(
            "❌ Buyurtma narxi xato."
        )

        return

    current = get_balance(uid)

    if current < price:

        await q.message.reply_text(
            "❌ Balans yetarli emas.\n\n"
            f"💰 Balans: {current:,.0f} so'm\n"
            f"💵 Kerak: {price:,.0f} so'm",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💳 Balans to'ldirish",
                        callback_data="deposit"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Bekor qilish",
                        callback_data="cancel"
                    )
                ]
            ])
        )

        return

    game_id = context.user_data.get(
        "game_id"
    )

    paket_id = context.user_data.get(
        "paket_id"
    )

    player_id = str(
        context.user_data.get(
            "player_id",
            ""
        )
    ).strip()

    server_id = str(
        context.user_data.get(
            "server_id",
            ""
        )
    ).strip()

    requires_server = bool(
        context.user_data.get(
            "requires_server",
            False
        )
    )

    if not player_id:

        await q.message.reply_text(
            "❌ Player/User ID kiritilmagan."
        )

        return

    if requires_server and not server_id:

        await q.message.reply_text(
            "❌ Server ID kiritilmagan."
        )

        return

    if game_id is None or paket_id is None:

        await q.message.reply_text(
            "❌ Buyurtma ma'lumotlari topilmadi."
        )

        return

    body = {
        "game_id": int(game_id),
        "paket_id": int(paket_id),
        "player_id": player_id
    }

    if requires_server:

        body["server_id"] = server_id

    log.info(
        "ORDER BODY: %s",
        body
    )

    # ========================================================
    # BALANSNI USHLAB TURISH
    # ========================================================

    add_balance(
        uid,
        -price,
        "order_hold",
        f"PlayPay buyurtma: {game_id}/{paket_id}"
    )

    # ========================================================
    # PLAYPAY
    # ========================================================

    status, data = await asyncio.to_thread(
        api_post,
        "/order",
        body
    )

    if not data.get("ok"):

        add_balance(
            uid,
            price,
            "order_refund",
            "PlayPay API xatosi: "
            f"{data.get('error','unknown')}"
        )

        err = data.get(
            "error",
            "API xatosi"
        )

        await q.message.reply_text(
            "❌ Buyurtma yuborilmadi.\n\n"
            f"Xato: {err}\n\n"
            f"💰 Pul balansga qaytarildi: "
            f"{price:,.0f} so'm",
            reply_markup=main_menu()
        )

        return

    playpay_id = data.get(
        "order_id",
        ""
    )

    order_status = data.get(
        "status",
        "processing"
    )

    api_price = data.get(
        "price",
        {}
    ) or {}

    charged = data.get(
        "charged",
        {}
    ) or {}

    cost_usd = Decimal(
        str(
            api_price.get(
                "amount",
                api_price.get(
                    "usd",
                    "0"
                )
            )
            or "0"
        )
    )

    charged_usd = Decimal(
        str(
            charged.get(
                "amount",
                charged.get(
                    "usd",
                    "0"
                )
            )
            or "0"
        )
    )

    c = conn()

    cur = c.execute(
        """
        INSERT INTO orders
        (
            user_id,
            playpay_order_id,
            game_id,
            paket_id,
            product_name,
            player_id,
            fields_json,
            cost_usd,
            charged_usd,
            sale_price,
            status,
            created_at,
            updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            uid,
            str(playpay_id),
            int(game_id),
            int(paket_id),
            context.user_data.get(
                "offer_name",
                "Paket"
            ),
            player_id,
            json.dumps(
                {
                    "player_id": player_id,
                    "server_id": server_id
                },
                ensure_ascii=False
            ),
            float(cost_usd),
            float(charged_usd),
            float(price),
            order_status,
            datetime.now().isoformat(),
            datetime.now().isoformat()
        )
    )

    local_order_id = cur.lastrowid

    c.commit()
    c.close()

    id_label = context.user_data.get(
        "id_label",
        "Player ID"
    )

    user_extra = ""

    if requires_server:

        user_extra = (
            f"🌐 Server ID: {server_id}\n"
        )

    await q.message.reply_text(
        f"✅ Buyurtma yuborildi!\n\n"
        f"📦 {context.user_data.get('offer_name','Paket')}\n"
        f"🆔 {id_label}: {player_id}\n"
        f"{user_extra}"
        f"💰 {price:,.0f} so'm\n"
        f"🔢 PlayPay order: {playpay_id}\n"
        f"📊 Status: {order_status}",
        reply_markup=main_menu()
    )

    # ========================================================
    # ADMIN
    # ========================================================

    try:

        await context.bot.send_message(
            bot_admin_id(context),
            f"🛒 YANGI BUYURTMA #{local_order_id}\n\n"
            f"👤 User ID: {uid}\n"
            f"🎮 Game ID: {game_id}\n"
            f"📦 {context.user_data.get('offer_name','Paket')}\n"
            f"🆔 {id_label}: {player_id}\n"
            +
            (
                f"🌐 Server ID: {server_id}\n"
                if requires_server
                else ""
            )
            +
            f"💰 Sotuv: {price:,.0f} so'm\n"
            f"🔢 PlayPay ID: {playpay_id}\n"
            f"📊 {order_status}\n"
            f"💵 API charged: {charged_usd} USD"
        )

    except Exception as e:

        log.error(
            "Admin buyurtma xabari xatosi: %s",
            e
        )

    # ========================================================
    # PROMO
    # ========================================================

    promo = context.user_data.get(
        "promo_code"
    )

    if promo:

        c = conn()

        exists = c.execute(
            """
            SELECT 1
            FROM promo_users
            WHERE user_id=?
              AND code=?
            """,
            (
                uid,
                promo
            )
        ).fetchone()

        if not exists:

            c.execute(
                """
                INSERT OR IGNORE INTO promo_users
                (user_id,code)
                VALUES (?,?)
                """,
                (
                    uid,
                    promo
                )
            )

            c.execute(
                """
                UPDATE promo_codes
                SET used=used+1
                WHERE code=?
                """,
                (promo,)
            )

        c.commit()
        c.close()

    context.user_data.clear()


# ============================================================
# CANCEL
# ============================================================

async def cancel(update, context):

    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        pass

    context.user_data.clear()

    await q.message.reply_text(
        "❌ Bekor qilindi.",
        reply_markup=main_menu()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance_cb(update, context):

    q = update.callback_query

    await q.message.reply_text(
        "💰 Balansingiz:\n\n"
        f"{get_balance(q.from_user.id):,.0f} so'm",
        reply_markup=main_menu()
    )


# ============================================================
# DEPOSIT
# ============================================================

async def deposit(update, context):

    q = update.callback_query

    context.user_data[
        "state"
    ] = "deposit_amount"

    card = get_setting(
        "payment_card",
        PAYMENT_CARD
    )

    await q.message.reply_text(
        "💳 Balans to'ldirish\n\n"
        f"Karta: `{card}`\n\n"
        "Qancha pul tashlamoqchisiz?\n"
        "Masalan: 50000",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Bekor qilish",
                    callback_data="cancel"
                )
            ]
        ])
    )


# ============================================================
# USER TEXT
# ============================================================

async def text_handler(update, context):

    u = update.effective_user

    ensure_user(u)

    text = update.message.text.strip()

    state = context.user_data.get(
        "state"
    )

    # ========================================================
    # PAYSTARS: STARS USERNAME
    # ========================================================
    if state == "ps_stars_username":
        name = text.lstrip("@")
        if not name or " " in name or len(name) > 64:
            await update.message.reply_text("❌ Username noto'g'ri. Masalan: @username")
            return
        try:
            r = await asyncio.to_thread(ps_check_user, name, "stars")
            if not r.get("valid"):
                raise RuntimeError("Username Stars uchun yaroqsiz.")
            context.user_data["ps_username"] = name
            context.user_data["ps_token"] = r.get("verification_token")
            context.user_data["state"] = "ps_stars_quantity"
            await update.message.reply_text("🔢 Nechta Stars?\n\nMinimal: 50")
        except Exception:
            await update.message.reply_text("❌ Username tekshirishda xatolik.")
        return

    if state == "ps_stars_quantity":
        try:
            quantity = int(text.replace(" ", ""))
        except Exception:
            await update.message.reply_text("❌ Faqat son yuboring.")
            return
        if quantity < 50:
            await update.message.reply_text("❌ Minimal 50 Stars.")
            return
        try:
            pricing = await asyncio.to_thread(ps_pricing)
            price = ps_sell(float(pricing.get("star_price", 0)) * quantity)
            if price <= 0:
                raise RuntimeError
            context.user_data["ps_quantity"] = quantity
            context.user_data["ps_price"] = price
            context.user_data["state"] = None
            await update.message.reply_text(
                f"⭐ Stars\n\n"
                f"👤 @{context.user_data['ps_username']}\n"
                f"⭐ {quantity}\n"
                f"💰 {price:,.0f} so'm\n"
                f"💳 Balans: {float(get_balance(u.id)):,.0f} so'm\n\n"
                "Tasdiqlaysizmi?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data="ps_confirm_stars"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"),
                ]])
            )
        except Exception:
            await update.message.reply_text("❌ Stars narxini olishda xatolik.")
        return

    # ========================================================
    # PAYSTARS: PREMIUM USERNAME
    # ========================================================
    if state == "ps_premium_username":
        name = text.lstrip("@")
        months = int(context.user_data.get("ps_months", 0))
        if not name or " " in name or months not in (3, 6, 12):
            await update.message.reply_text("❌ Username noto'g'ri.")
            return
        try:
            r = await asyncio.to_thread(ps_check_user, name, "premium")
            if not r.get("valid"):
                raise RuntimeError
            pricing = await asyncio.to_thread(ps_pricing)
            price = ps_sell(float(pricing.get(f"premium_{months}_price", 0)))
            if price <= 0:
                raise RuntimeError
            context.user_data["ps_username"] = name
            context.user_data["ps_token"] = r.get("verification_token")
            context.user_data["ps_price"] = price
            context.user_data["state"] = None
            await update.message.reply_text(
                f"💎 Premium\n\n"
                f"👤 @{name}\n"
                f"💎 {months} oy\n"
                f"💰 {price:,.0f} so'm\n"
                f"💳 Balans: {float(get_balance(u.id)):,.0f} so'm\n\n"
                "Tasdiqlaysizmi?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data="ps_confirm_premium"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"),
                ]])
            )
        except Exception:
            await update.message.reply_text("❌ Premium ma'lumotlarini olishda xatolik.")
        return

    # ========================================================
    # BOT TOKEN QO'SHISH
    # ========================================================
    if state == "add_bot_token":
        try:
            me, created = await create_child_bot(u.id, u.username or "", text)
            context.user_data.clear()
            status = child_bot_row(me["id"])
            bot_action = "✅ Bot qo'shildi!" if created else "✅ Bot yangilandi!"
            await update.message.reply_text(
                f"{bot_action}\n\n"
                f"🤖 @{me.get('username','')}\n"
                f"🆔 Bot ID: {me['id']}\n"
                f"🧪 Sinov: 1 kun\n"
                f"⏰ Sinov tugashi: {format_dt(status['trial_until'])}\n\n"
                "Obuna faol bo'lmasa bot ishlamaydi.",
                reply_markup=main_menu()
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Bot qo'shib bo'lmadi:\n{e}")
        return

    if state == "bot_markup":
        try:
            amount=Decimal(text.replace(" ","").replace(",",""))
            if amount < 0: raise ValueError
            bot_id=int(context.user_data.get("settings_bot_id"))
        except Exception:
            await update.message.reply_text("❌ UZS summani to'g'ri yuboring. Masalan: 3000")
            return
        c=main_conn(); c.execute("UPDATE child_bots SET markup_uzs=? WHERE bot_id=? AND owner_user_id=?",(float(amount),bot_id,u.id)); c.commit(); c.close()
        context.user_data.clear()
        await update.message.reply_text(f"✅ Ustama saqlandi: {amount:,.0f} so'm",reply_markup=main_menu())
        return

    # ========================================================
    # DEPOSIT
    # ========================================================

    if state == "deposit_amount":

        try:

            amount = Decimal(
                text.replace(",", "")
                .replace(" ", "")
            )

        except InvalidOperation:

            await update.message.reply_text(
                "❌ Summani raqamda yuboring."
            )

            return

        if amount <= 0:

            await update.message.reply_text(
                "❌ Noto'g'ri summa."
            )

            return

        context.user_data.update({
            "state": "waiting_receipt",
            "deposit_amount": float(amount)
        })

        card = get_setting(
            "payment_card",
            PAYMENT_CARD
        )

        await update.message.reply_text(
            f"💳 To'lov kartasi:\n\n"
            f"`{card}`\n\n"
            f"💰 Tashlaydigan summa: "
            f"{amount:,.0f} so'm\n\n"
            "⚠️ Aynan shu summani tashlang.\n"
            "To'lovdan keyin 📸 chek rasmini yuboring.\n\n"
            "Chek admin tomonidan tekshiriladi.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Bekor qilish",
                        callback_data="cancel"
                    )
                ]
            ])
        )

        return

    # ========================================================
    # PROMO
    # ========================================================

    if state == "promo":

        code = text.upper()

        c = conn()

        r = c.execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code=?
              AND active=1
            """,
            (code,)
        ).fetchone()

        used = c.execute(
            """
            SELECT 1
            FROM promo_users
            WHERE user_id=?
              AND code=?
            """,
            (
                u.id,
                code
            )
        ).fetchone()

        c.close()

        if not r:

            await update.message.reply_text(
                "❌ Promo kod noto'g'ri."
            )

            return

        if (
            r["max_uses"] > 0
            and
            r["used"] >= r["max_uses"]
        ):

            await update.message.reply_text(
                "❌ Promo kodi limiti tugagan."
            )

            return

        if used:

            await update.message.reply_text(
                "❌ Bu promo koddan oldin foydalangansiz."
            )

            return

        context.user_data[
            "promo_code"
        ] = code

        context.user_data[
            "state"
        ] = None

        await update.message.reply_text(
            f"✅ {code} qabul qilindi!\n"
            f"🎁 Chegirma: {r['percent']}%"
        )

        return

    # ========================================================
    # PLAYER / USER ID
    # ========================================================

    if state == "player_id":

        if len(text) > 100:

            await update.message.reply_text(
                "❌ ID juda uzun."
            )

            return

        context.user_data[
            "player_id"
        ] = text

        if context.user_data.get(
            "requires_server",
            False
        ):

            context.user_data[
                "state"
            ] = "server_id"

            await update.message.reply_text(
                "🌐 Server ID ni yuboring:\n\n"
                "Masalan: 1234"
            )

            return

        context.user_data[
            "state"
        ] = None

        await confirm_order(
            update.message,
            context
        )

        return

    # ========================================================
    # SERVER ID
    # ========================================================

    if state == "server_id":

        if len(text) > 100:

            await update.message.reply_text(
                "❌ Server ID juda uzun."
            )

            return

        context.user_data[
            "server_id"
        ] = text

        context.user_data[
            "state"
        ] = None

        await confirm_order(
            update.message,
            context
        )

        return


# ============================================================
# RECEIPT
# ============================================================

async def photo_handler(update, context):

    u = update.effective_user

    ensure_user(u)

    if context.user_data.get(
        "state"
    ) != "waiting_receipt":

        return

    amount = Decimal(
        str(
            context.user_data.get(
                "deposit_amount",
                0
            )
        )
    )

    if amount <= 0:

        await update.message.reply_text(
            "❌ Summa xatosi."
        )

        return

    photo_id = (
        update.message.photo[-1].file_id
    )

    c = conn()

    cur = c.execute(
        """
        INSERT INTO payments
        (
            user_id,
            requested_amount,
            photo_id,
            status,
            created_at
        )
        VALUES (?,?,?,'pending',?)
        """,
        (
            u.id,
            float(amount),
            photo_id,
            datetime.now().isoformat()
        )
    )

    pid = cur.lastrowid

    c.commit()
    c.close()

    await update.message.reply_text(
        "✅ Chek adminga yuborildi.\n\n"
        "Admin tekshirganidan keyin balansingizga "
        "tasdiqlangan summa qo'shiladi."
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Qabul qilish",
                callback_data=f"payok:{pid}"
            ),
            InlineKeyboardButton(
                "❌ Rad etish",
                callback_data=f"payno:{pid}"
            )
        ]
    ])

    await context.bot.send_photo(
        bot_admin_id(context),
        photo_id,
        caption=(
            f"💳 TO'LOV #{pid}\n\n"
            f"👤 User ID: {u.id}\n"
            f"👤 @{u.username or 'username yo‘q'}\n"
            f"💰 So'ralgan: "
            f"{amount:,.0f} so'm\n"
            f"🕐 "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ),
        reply_markup=kb
    )

    context.user_data.clear()


# ============================================================
# PAYMENT ACTION
# ============================================================

async def payment_action(update, context):

    q = update.callback_query

    if q.from_user.id != bot_admin_id(context):

        await q.answer(
            "Siz admin emassiz.",
            show_alert=True
        )

        return

    try:
        await q.answer()
    except Exception:
        pass

    action, pid_text = q.data.split(
        ":",
        1
    )

    try:

        pid = int(pid_text)

    except Exception:

        await q.message.reply_text(
            "❌ To'lov ID xato."
        )

        return

    c = conn()

    payment = c.execute(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (pid,)
    ).fetchone()

    c.close()

    if not payment:

        await q.message.reply_text(
            "❌ To'lov topilmadi."
        )

        return

    if payment["status"] != "pending":

        await q.message.reply_text(
            "⚠️ Bu to'lov allaqachon ko'rilgan."
        )

        return

    if action == "payno":

        c = conn()

        c.execute(
            """
            UPDATE payments
            SET status='rejected',
                approved_at=?
            WHERE id=?
            """,
            (
                datetime.now().isoformat(),
                pid
            )
        )

        c.commit()
        c.close()

        await context.bot.send_message(
            payment["user_id"],
            "❌ To'lovingiz admin tomonidan rad etildi."
        )

        await q.message.reply_text(
            "❌ To'lov rad etildi."
        )

        return

    context.user_data[
        "admin_state"
    ] = "approve_payment"

    context.user_data[
        "payment_id"
    ] = pid

    await q.message.reply_text(
        f"💳 To'lov #{pid}\n\n"
        f"👤 User: {payment['user_id']}\n"
        f"💰 So'ralgan: "
        f"{payment['requested_amount']:,.0f} so'm\n\n"
        "Balansga qancha qo'shilsin?\n"
        "Masalan: 50000"
    )


# ============================================================
# PROFILE
# ============================================================

async def profile(update, context):

    q = update.callback_query

    c = conn()

    r = c.execute(
        """
        SELECT *
        FROM users
        WHERE user_id=?
        """,
        (
            q.from_user.id,
        )
    ).fetchone()

    orders = c.execute(
        """
        SELECT COUNT(*) AS x
        FROM orders
        WHERE user_id=?
        """,
        (
            q.from_user.id,
        )
    ).fetchone()["x"]

    c.close()

    if not r:
        return

    created = datetime.fromisoformat(
        r["created_at"]
    )

    await q.message.reply_text(
        f"👤 PROFIL\n\n"
        f"🆔 ID: {r['user_id']}\n"
        f"👤 Username: @{r['username'] or 'yo‘q'}\n"
        f"💰 Balans: {r['balance']:,.0f} so'm\n"
        f"📦 Buyurtmalar: {orders}\n"
        f"📅 Sana: {created.strftime('%d.%m.%Y')}\n"
        f"⏰ Vaqt: {created.strftime('%H:%M')}",
        reply_markup=main_menu()
    )


# ============================================================
# ORDERS
# ============================================================

async def orders_cb(update, context):

    q = update.callback_query

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM orders
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 20
        """,
        (
            q.from_user.id,
        )
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "📦 Buyurtmalar yo'q."
        )

        return

    text = "📦 BUYURTMALARIM\n\n"

    for r in rows:

        fields = {}

        try:

            fields = json.loads(
                r["fields_json"] or "{}"
            )

        except Exception:

            pass

        server = fields.get(
            "server_id",
            ""
        )

        text += (
            f"#{r['id']} — "
            f"{r['product_name']}\n"
            f"🆔 {r['player_id']}\n"
        )

        if server:

            text += (
                f"🌐 Server ID: {server}\n"
            )

        text += (
            f"💰 {r['sale_price']:,.0f} so'm\n"
            f"📊 {r['status']}\n"
            f"🔢 PlayPay: "
            f"{r['playpay_order_id']}\n"
            f"🕐 {r['created_at']}\n\n"
        )

    await q.message.reply_text(
        text
    )


# ============================================================
# PROMO
# ============================================================

async def promo_cb(update, context):

    q = update.callback_query

    context.user_data[
        "state"
    ] = "promo"

    await q.message.reply_text(
        "🎁 Promo kodni yuboring:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Bekor qilish",
                    callback_data="cancel"
                )
            ]
        ])
    )


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(update, context):

    set_request_db(context)
    if update.effective_user.id != bot_admin_id(context):

        await update.message.reply_text(
            "❌ Siz admin emassiz."
        )

        return

    await update.message.reply_text(
        "👑 ADMIN PANEL",
        reply_markup=admin_kb()
    )


# ============================================================
# ADMIN STATS
# ============================================================

def sum_history(
    start,
    end=None,
    positive=True
):

    c = conn()

    op = ">" if positive else "<"

    if end:

        r = c.execute(
            f"""
            SELECT COALESCE(SUM(amount),0)
            FROM balance_history
            WHERE created_at>=?
              AND created_at<?
              AND amount {op} 0
            """,
            (
                start,
                end
            )
        ).fetchone()

    else:

        r = c.execute(
            f"""
            SELECT COALESCE(SUM(amount),0)
            FROM balance_history
            WHERE created_at>=?
              AND amount {op} 0
            """,
            (
                start,
            )
        ).fetchone()

    c.close()

    value = Decimal(
        str(r[0] or 0)
    )

    return (
        value
        if positive
        else abs(value)
    )


def period_stats(
    days=None,
    exact_day=False
):

    now = datetime.now()

    if exact_day:

        start = datetime(
            now.year,
            now.month,
            now.day
        )

        end = start + timedelta(
            days=1
        )

    else:

        start = (
            now -
            timedelta(days=days)
        )

        end = None

    income = sum_history(
        start.isoformat(),
        (
            end.isoformat()
            if end
            else None
        ),
        True
    )

    outgoing = sum_history(
        start.isoformat(),
        (
            end.isoformat()
            if end
            else None
        ),
        False
    )

    return income, outgoing


async def admin_stats(update, context):

    q = update.callback_query

    today_income, today_out = period_stats(
        exact_day=True
    )

    today = datetime.now().date()

    ystart = datetime.combine(
        today - timedelta(days=1),
        datetime.min.time()
    )

    yend = datetime.combine(
        today,
        datetime.min.time()
    )

    yesterday_income = sum_history(
        ystart.isoformat(),
        yend.isoformat(),
        True
    )

    yesterday_out = sum_history(
        ystart.isoformat(),
        yend.isoformat(),
        False
    )

    week_income, week_out = period_stats(
        days=7
    )

    month_income, month_out = period_stats(
        days=30
    )

    c = conn()

    total_users = c.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    today_users = c.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE created_at>=?
        """,
        (
            datetime.combine(
                today,
                datetime.min.time()
            ).isoformat(),
        )
    ).fetchone()[0]

    week_users = c.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE created_at>=?
        """,
        (
            (
                datetime.now()
                -
                timedelta(days=7)
            ).isoformat(),
        )
    ).fetchone()[0]

    month_users = c.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE created_at>=?
        """,
        (
            (
                datetime.now()
                -
                timedelta(days=30)
            ).isoformat(),
        )
    ).fetchone()[0]

    total_orders = c.execute(
        "SELECT COUNT(*) FROM orders"
    ).fetchone()[0]

    today_orders = c.execute(
        """
        SELECT COUNT(*)
        FROM orders
        WHERE created_at>=?
        """,
        (
            datetime.combine(
                today,
                datetime.min.time()
            ).isoformat(),
        )
    ).fetchone()[0]

    total_balance = c.execute(
        """
        SELECT COALESCE(SUM(balance),0)
        FROM users
        """
    ).fetchone()[0]

    c.close()

    await q.message.reply_text(
        "📊 ADMIN STATISTIKA\n\n"
        "🟢 BUGUN\n"
        f"💵 Kirim: {today_income:,.0f} so'm\n"
        f"🔴 Chiqim: {today_out:,.0f} so'm\n"
        f"👥 Yangi user: {today_users}\n"
        f"📦 Buyurtma: {today_orders}\n\n"
        "🟡 KECHA\n"
        f"💵 Kirim: {yesterday_income:,.0f} so'm\n"
        f"🔴 Chiqim: {yesterday_out:,.0f} so'm\n\n"
        "🔵 1 HAFTA\n"
        f"💵 Kirim: {week_income:,.0f} so'm\n"
        f"🔴 Chiqim: {week_out:,.0f} so'm\n"
        f"👥 Yangi user: {week_users}\n\n"
        "🟣 1 OY\n"
        f"💵 Kirim: {month_income:,.0f} so'm\n"
        f"🔴 Chiqim: {month_out:,.0f} so'm\n"
        f"👥 Yangi user: {month_users}\n\n"
        "📌 UMUMIY\n"
        f"👥 Jami user: {total_users}\n"
        f"📦 Jami buyurtma: {total_orders}\n"
        f"💰 Userlar balanslari jami: "
        f"{total_balance:,.0f} so'm"
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def admin_users(update, context):

    q = update.callback_query

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM users
        ORDER BY created_at DESC
        LIMIT 50
        """
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "👥 Foydalanuvchilar yo'q."
        )

        return

    text = "👥 FOYDALANUVCHILAR\n\n"

    for r in rows:

        text += (
            f"👤 {r['first_name'] or 'User'}\n"
            f"🆔 ID: {r['user_id']}\n"
            f"🔗 @{r['username'] or 'yo‘q'}\n"
            f"💰 Balans: "
            f"{r['balance']:,.0f} so'm\n"
            f"📅 {r['created_at']}\n\n"
        )

    await q.message.reply_text(
        text
    )


# ============================================================
# ADMIN BALANCE
# ============================================================

async def admin_addbalance_start(
    update,
    context
):

    q = update.callback_query

    context.user_data.clear()

    context.user_data[
        "admin_state"
    ] = "balance_user"

    await q.message.reply_text(
        "👤 User ID yuboring.\n\n"
        "Keyin + yoki - summa kiritasiz."
    )


# ============================================================
# ADMIN PAYMENTS
# ============================================================

async def admin_payments(update, context):

    q = update.callback_query

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM payments
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "💳 To'lovlar yo'q."
        )

        return

    text = "💳 TO'LOVLAR\n\n"

    for r in rows:

        text += (
            f"#{r['id']} | User: {r['user_id']}\n"
            f"💰 So'ralgan: "
            f"{r['requested_amount']:,.0f}\n"
            f"✅ Tasdiqlangan: "
            f"{r['approved_amount']:,.0f}\n"
            f"📊 {r['status']}\n"
            f"🕐 {r['created_at']}\n\n"
        )

    await q.message.reply_text(
        text
    )


# ============================================================
# ADMIN ORDERS
# ============================================================

async def admin_orders(update, context):

    q = update.callback_query

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM orders
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "📦 Buyurtmalar yo'q."
        )

        return

    text = "📦 BUYURTMALAR\n\n"

    for r in rows:

        fields = {}

        try:

            fields = json.loads(
                r["fields_json"] or "{}"
            )

        except Exception:

            pass

        text += (
            f"#{r['id']}\n"
            f"👤 User: {r['user_id']}\n"
            f"🎮 Game ID: {r['game_id']}\n"
            f"📦 {r['product_name']}\n"
            f"🆔 Player/User ID: {r['player_id']}\n"
        )

        if fields.get("server_id"):

            text += (
                f"🌐 Server ID: "
                f"{fields['server_id']}\n"
            )

        text += (
            f"💰 Sotuv: "
            f"{r['sale_price']:,.0f} so'm\n"
            f"📊 {r['status']}\n"
            f"🔢 PlayPay: "
            f"{r['playpay_order_id']}\n"
            f"🕐 {r['created_at']}\n\n"
        )

    await q.message.reply_text(
        text
    )


# ============================================================
# ADMIN PRICES
# ============================================================

async def admin_prices(update, context):

    q = update.callback_query

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM products
        WHERE active=1
        ORDER BY game_name, package_name
        LIMIT 100
        """
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "❌ Avval 🔄 Katalog tugmasini bosing."
        )

        return

    kb = []

    for r in rows:

        kb.append([
            InlineKeyboardButton(
                f"{r['game_name'][:14]} | "
                f"{r['package_name'][:18]} | "
                f"{r['sale_price']:,.0f}",
                callback_data=(
                    f"price:"
                    f"{r['game_id']}:"
                    f"{r['paket_id']}"
                )
            )
        ])

    await q.message.reply_text(
        "💵 O'zgartiriladigan paketni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            kb
        )
    )


async def price_callback(update, context):

    q = update.callback_query

    if q.from_user.id != bot_admin_id(context):

        await q.answer(
            "Siz admin emassiz.",
            show_alert=True
        )

        return

    try:

        _, gid, pid = q.data.split(
            ":",
            2
        )

        game_id = int(gid)
        paket_id = int(pid)

    except Exception:

        await q.message.reply_text(
            "❌ ID xato."
        )

        return

    c = conn()

    r = c.execute(
        """
        SELECT *
        FROM products
        WHERE game_id=?
          AND paket_id=?
        """,
        (
            game_id,
            paket_id
        )
    ).fetchone()

    c.close()

    if not r:

        await q.message.reply_text(
            "❌ Mahsulot topilmadi."
        )

        return

    context.user_data.clear()

    context.user_data[
        "admin_state"
    ] = "set_price"

    context.user_data[
        "price_key"
    ] = (
        game_id,
        paket_id
    )

    await q.message.reply_text(
        f"📦 {r['game_name']}\n"
        f"🎁 {r['package_name']}\n"
        f"💰 Hozirgi: "
        f"{r['sale_price']:,.0f} so'm\n\n"
        "Yangi sotuv narxini yuboring:"
    )


# ============================================================
# ADMIN PROMO / CARD / POST
# ============================================================

async def admin_promo_start(update, context):

    q = update.callback_query

    context.user_data.clear()

    context.user_data[
        "admin_state"
    ] = "promo_admin"

    await q.message.reply_text(
        "🎁 Promo yaratish:\n\n"
        "KOD FOIZ LIMIT\n\n"
        "Misol: SALE10 10 100\n"
        "0 limit = cheksiz"
    )


async def admin_card_start(update, context):

    q = update.callback_query

    context.user_data.clear()

    context.user_data[
        "admin_state"
    ] = "set_card"

    await q.message.reply_text(
        f"💳 Hozirgi karta:\n"
        f"{get_setting('payment_card', PAYMENT_CARD)}\n\n"
        "Yangi karta raqamini yuboring:"
    )


async def admin_post_start(update, context):

    q = update.callback_query

    context.user_data.clear()

    context.user_data[
        "admin_state"
    ] = "post_content"

    await q.message.reply_text(
        "📢 Kanalga post yuborish.\n\n"
        "Avval post matnini yuboring.\n"
        "Keyin rasm/GIF/video yuboring.\n\n"
        "Faqat matn bo'lsa MATN deb yozing."
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text_handler(update, context):

    if update.effective_user.id != bot_admin_id(context):

        return False

    state = context.user_data.get(
        "admin_state"
    )

    text = update.message.text.strip()

    if not state:

        return False

    # ========================================================
    # BALANCE USER
    # ========================================================

    if state == "balance_user":

        try:

            uid = int(text)

        except Exception:

            await update.message.reply_text(
                "❌ User ID raqam bo'lishi kerak."
            )

            return True

        if not user_exists(uid):

            await update.message.reply_text(
                "❌ User topilmadi."
            )

            return True

        context.user_data[
            "balance_user"
        ] = uid

        context.user_data[
            "admin_state"
        ] = "balance_amount"

        await update.message.reply_text(
            "➕ Qo'shish: +50000\n"
            "➖ Ayirish: -50000\n\n"
            "Misol: +50000"
        )

        return True

    # ========================================================
    # BALANCE AMOUNT
    # ========================================================

    if state == "balance_amount":

        try:

            amount = Decimal(
                text.replace(",", "")
                .replace(" ", "")
            )

        except InvalidOperation:

            await update.message.reply_text(
                "❌ Masalan +50000 yoki -50000 yozing."
            )

            return True

        if amount == 0:

            await update.message.reply_text(
                "❌ 0 mumkin emas."
            )

            return True

        uid = context.user_data[
            "balance_user"
        ]

        if (
            amount < 0
            and
            get_balance(uid) < abs(amount)
        ):

            await update.message.reply_text(
                "❌ User balansida buncha pul yo'q."
            )

            return True

        add_balance(
            uid,
            amount,
            (
                "admin_add"
                if amount > 0
                else "admin_remove"
            ),
            "Admin tomonidan balans o'zgartirildi"
        )

        new_balance = get_balance(
            uid
        )

        try:

            await context.bot.send_message(
                uid,
                f"👑 Admin balansingizni o'zgartirdi.\n\n"
                f"{'➕' if amount > 0 else '➖'} "
                f"{abs(amount):,.0f} so'm\n"
                f"💰 Yangi balans: "
                f"{new_balance:,.0f} so'm"
            )

        except Exception:

            pass

        await update.message.reply_text(
            f"✅ Bajarildi.\n"
            f"👤 {uid}\n"
            f"{'➕' if amount > 0 else '➖'} "
            f"{abs(amount):,.0f} so'm\n"
            f"💰 Yangi balans: "
            f"{new_balance:,.0f} so'm",
            reply_markup=admin_kb()
        )

        context.user_data.clear()

        return True

    # ========================================================
    # APPROVE PAYMENT
    # ========================================================

    if state == "approve_payment":

        try:

            amount = Decimal(
                text.replace(",", "")
                .replace(" ", "")
            )

        except InvalidOperation:

            await update.message.reply_text(
                "❌ Faqat raqam yozing."
            )

            return True

        if amount <= 0:

            await update.message.reply_text(
                "❌ Summa 0 dan katta bo'lsin."
            )

            return True

        pid = context.user_data[
            "payment_id"
        ]

        c = conn()

        payment = c.execute(
            """
            SELECT *
            FROM payments
            WHERE id=?
            """,
            (pid,)
        ).fetchone()

        if not payment:

            c.close()

            context.user_data.clear()

            await update.message.reply_text(
                "❌ To'lov topilmadi."
            )

            return True

        if payment["status"] != "pending":

            c.close()

            context.user_data.clear()

            await update.message.reply_text(
                "⚠️ Bu to'lov allaqachon ko'rilgan."
            )

            return True

        c.execute(
            """
            UPDATE payments
            SET status='approved',
                approved_amount=?,
                approved_at=?
            WHERE id=?
            """,
            (
                float(amount),
                datetime.now().isoformat(),
                pid
            )
        )

        c.commit()
        c.close()

        add_balance(
            payment["user_id"],
            amount,
            "deposit",
            f"To'lov #{pid} tasdiqlandi"
        )

        new_balance = get_balance(
            payment["user_id"]
        )

        try:

            await context.bot.send_message(
                payment["user_id"],
                f"✅ To'lov tasdiqlandi!\n\n"
                f"➕ Balansga: "
                f"{amount:,.0f} so'm\n"
                f"💰 Yangi balans: "
                f"{new_balance:,.0f} so'm"
            )

        except Exception:

            pass

        await update.message.reply_text(
            f"✅ Balans qo'shildi.\n"
            f"👤 {payment['user_id']}\n"
            f"➕ {amount:,.0f} so'm",
            reply_markup=admin_kb()
        )

        context.user_data.clear()

        return True

    # ========================================================
    # SET PRICE
    # ========================================================

    if state == "set_price":

        try:

            price = Decimal(
                text.replace(",", "")
                .replace(" ", "")
            )

        except InvalidOperation:

            await update.message.reply_text(
                "❌ Narx raqam bo'lishi kerak."
            )

            return True

        if price < 0:

            await update.message.reply_text(
                "❌ Narx 0 yoki undan katta bo'lsin."
            )

            return True

        game_id, paket_id = (
            context.user_data[
                "price_key"
            ]
        )

        c = conn()

        c.execute(
            """
            UPDATE products
            SET sale_price=?,
                updated_at=?
            WHERE game_id=?
              AND paket_id=?
            """,
            (
                float(price),
                datetime.now().isoformat(),
                game_id,
                paket_id
            )
        )

        c.commit()
        c.close()

        # Admin belgilagan baza narxini barcha mijoz botlariga bir xil tarqatamiz.
        propagate_product_price(game_id, paket_id, price)

        await update.message.reply_text(
            f"✅ Baza narx o'zgartirildi: {price:,.0f} so'm\n"
            "🤖 Barcha mijoz botlariga yangilandi.",
            reply_markup=admin_kb()
        )

        context.user_data.clear()

        return True

    # ========================================================
    # CARD
    # ========================================================

    if state == "set_card":

        set_setting(
            "payment_card",
            text
        )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Karta saqlandi:\n"
            f"{get_setting('payment_card')}",
            reply_markup=admin_kb()
        )

        return True

    # ========================================================
    # PROMO ADMIN
    # ========================================================

    if state == "promo_admin":

        parts = text.split()

        if len(parts) != 3:

            await update.message.reply_text(
                "Format: SALE10 10 100"
            )

            return True

        code = parts[0].upper()

        try:

            percent = float(parts[1])
            limit = int(parts[2])

        except Exception:

            await update.message.reply_text(
                "❌ Foiz va limit raqam bo'lsin."
            )

            return True

        if (
            percent <= 0
            or percent > 100
            or limit < 0
        ):

            await update.message.reply_text(
                "❌ Qiymatlar noto'g'ri."
            )

            return True

        c = conn()

        c.execute(
            """
            INSERT OR REPLACE INTO promo_codes
            (code,percent,max_uses,used,active)
            VALUES (?,?,?,0,1)
            """,
            (
                code,
                percent,
                limit
            )
        )

        c.commit()
        c.close()

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Promo yaratildi!\n"
            f"🎁 {code}\n"
            f"💸 {percent}%\n"
            f"🔢 Limit: {limit}",
            reply_markup=admin_kb()
        )

        return True

    # ========================================================
    # POST CONTENT
    # ========================================================

    if state == "post_content":

        context.user_data[
            "post_text"
        ] = text

        context.user_data[
            "admin_state"
        ] = "post_wait_media"

        await update.message.reply_text(
            "✅ Matn saqlandi.\n\n"
            "Endi rasm/GIF/video yuboring.\n"
            "Faqat matnli post bo'lsa MATN deb yozing."
        )

        return True

    # ========================================================
    # TEXT POST
    # ========================================================

    if (
        state == "post_wait_media"
        and
        text.upper() == "MATN"
    ):

        try:

            await send_channel_post(
                context,
                text=context.user_data.get(
                    "post_text",
                    ""
                )
            )

            context.user_data.clear()

            await update.message.reply_text(
                "✅ Matnli post yuborildi.",
                reply_markup=admin_kb()
            )

        except Exception as e:

            await update.message.reply_text(
                f"❌ {e}"
            )

        return True

    return False


# ============================================================
# CHANNEL POST
# ============================================================

async def send_channel_post(
    context,
    text="",
    photo_id=None,
    animation_id=None,
    video_id=None
):

    channel_id = get_setting(
        "channel_id",
        ""
    )

    if not channel_id:

        raise RuntimeError(
            "channel_id sozlanmagan."
        )

    if photo_id:

        await context.bot.send_photo(
            channel_id,
            photo_id,
            caption=text or None
        )

    elif animation_id:

        await context.bot.send_animation(
            channel_id,
            animation_id,
            caption=text or None
        )

    elif video_id:

        await context.bot.send_video(
            channel_id,
            video_id,
            caption=text or None
        )

    else:

        await context.bot.send_message(
            channel_id,
            text=text
        )


async def admin_media_handler(
    update,
    context
):

    if update.effective_user.id != bot_admin_id(context):

        return

    if context.user_data.get(
        "admin_state"
    ) != "post_wait_media":

        return

    caption = context.user_data.get(
        "post_text",
        ""
    )

    try:

        if update.message.photo:

            await send_channel_post(
                context,
                text=caption,
                photo_id=(
                    update.message
                    .photo[-1]
                    .file_id
                )
            )

        elif update.message.animation:

            await send_channel_post(
                context,
                text=caption,
                animation_id=(
                    update.message
                    .animation
                    .file_id
                )
            )

        elif update.message.video:

            await send_channel_post(
                context,
                text=caption,
                video_id=(
                    update.message
                    .video
                    .file_id
                )
            )

        else:

            return

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Post kanalga yuborildi.",
            reply_markup=admin_kb()
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ Post yuborilmadi: {e}"
        )


# ============================================================
# ORDER STATUS
# ============================================================

async def check_orders(context):

    set_request_db(context)
    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM orders
        WHERE status IN
        ('processing','pending')
          AND playpay_order_id IS NOT NULL
          AND playpay_order_id!=''
        ORDER BY id ASC
        LIMIT 30
        """
    ).fetchall()

    c.close()

    for r in rows:

        try:

            status, data = await asyncio.to_thread(
                get_playpay_order,
                r["playpay_order_id"]
            )

            if not data.get("ok"):

                continue

            order = data.get(
                "order",
                data
            )

            new_status = order.get(
                "status",
                r["status"]
            )

            if new_status == r["status"]:

                continue

            c = conn()

            c.execute(
                """
                UPDATE orders
                SET status=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    new_status,
                    datetime.now().isoformat(),
                    r["id"]
                )
            )

            c.commit()
            c.close()

            await context.bot.send_message(
                r["user_id"],
                f"📦 Buyurtma #{r['id']}\n\n"
                f"📊 Yangi status: {new_status}"
            )

        except Exception as e:

            log.error(
                "Order status xatosi: %s",
                e
            )


# ============================================================
# RATING
# ============================================================

async def rating_callback(update, context):

    q = update.callback_query

    if q.from_user.id != bot_admin_id(context):

        return

    period = q.data.split(
        ":"
    )[1]

    days = (
        7
        if period == "week"
        else 30
    )

    start = (
        datetime.now()
        -
        timedelta(days=days)
    ).isoformat()

    c = conn()

    rows = c.execute(
        """
        SELECT
            u.user_id,
            u.username,
            u.first_name,
            COUNT(o.id) AS orders,
            COALESCE(
                SUM(o.sale_price),
                0
            ) AS spent
        FROM users u
        LEFT JOIN orders o
          ON u.user_id=o.user_id
         AND o.created_at>=?
        GROUP BY u.user_id
        ORDER BY spent DESC
        LIMIT 20
        """,
        (
            start,
        )
    ).fetchall()

    c.close()

    title = (
        "1 HAFTALIK"
        if period == "week"
        else "1 OYLIK"
    )

    text = (
        f"🏆 {title} REYTING\n\n"
    )

    n = 1

    for r in rows:

        if r["spent"] <= 0:

            continue

        text += (
            f"{n}. "
            f"{r['first_name'] or 'User'} "
            f"(@{r['username'] or 'yo‘q'})\n"
            f"🆔 {r['user_id']}\n"
            f"📦 Buyurtma: {r['orders']}\n"
            f"💰 Xarid: "
            f"{r['spent']:,.0f} so'm\n\n"
        )

        n += 1

    if n == 1:

        text += "Hali ma'lumot yo'q."

    await q.message.reply_text(
        text
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):

    q = update.callback_query
    if q.from_user.id != bot_admin_id(context):

        return

    d = q.data

    if d == "adm_addbalance":

        await admin_addbalance_start(
            update,
            context
        )

    elif d == "adm_payments":

        await admin_payments(
            update,
            context
        )

    elif d == "adm_stats":

        await admin_stats(
            update,
            context
        )

    elif d == "adm_rating":

        await q.message.reply_text(
            "🏆 Reytingni tanlang:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🏆 1 haftalik",
                        callback_data="rating:week"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏆 1 oylik",
                        callback_data="rating:month"
                    )
                ]
            ])
        )

    elif d == "adm_users":

        await admin_users(
            update,
            context
        )

    elif d == "adm_orders":

        await admin_orders(
            update,
            context
        )

    elif d == "adm_prices":

        await admin_prices(
            update,
            context
        )

    elif d == "adm_promo":

        await admin_promo_start(
            update,
            context
        )

    elif d == "adm_card":

        await admin_card_start(
            update,
            context
        )

    elif d == "adm_post":

        await admin_post_start(
            update,
            context
        )

    elif d == "adm_playpay_balance":

        status, data = await asyncio.to_thread(
            get_playpay_balance
        )

        if data.get("ok"):

            b = data.get(
                "balance",
                {}
            )

            await q.message.reply_text(
                f"🔐 PLAYPAY BALANSI\n\n"
                f"💵 USD: "
                f"{b.get('amount', b.get('usd','0'))}\n"
                f"💱 Valyuta: "
                f"{b.get('currency','USD')}"
            )

        else:

            await q.message.reply_text(
                "❌ PlayPay balansini olishda xato:\n"
                f"{data.get('error','API xatosi')}"
            )

    # ========================================================
    # FAQAT ADMIN KATALOG YANGILAYDI
    # ========================================================

    elif d == "a_sync":

        await q.message.reply_text(
            "🔄 PlayPay katalogi yangilanmoqda..."
        )

        ok, result = await asyncio.to_thread(
            sync_catalog
        )
        if ok:
            sync_all_child_catalogs()

        await q.message.reply_text(
            f"{'✅' if ok else '❌'} {result}",
            reply_markup=admin_kb()
        )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):

    set_request_db(context)
    q = update.callback_query
    if not await child_platform_access(update, context):
        return

    try:
        await q.answer()
    except Exception:
        pass

    d = q.data

    # Qo'shimcha providerlar
    if d == "paystars":
        return await paystars_menu(update, context)
    if d == "ps_stars":
        return await ps_stars_start(update, context)
    if d == "ps_premium":
        return await ps_premium_start(update, context)
    if d in ("ps_pm_3", "ps_pm_6", "ps_pm_12"):
        return await ps_premium_month(update, context)
    if d == "ps_confirm_stars":
        return await ps_confirm(update, context, "stars")
    if d == "ps_confirm_premium":
        return await ps_confirm(update, context, "premium")
    if d == "aktivsim_buy":
        return await aktivsim_countries_handler(update, context)
    if d.startswith("as_country_"):
        return await aktivsim_country_handler(update, context)
    if d == "adm_paystars_balance":
        return await paystars_balance_admin(update, context)
    if d == "adm_aktivsim_balance":
        return await aktivsim_balance_admin(update, context)
    if d == "subscription":
        return await subscription_menu(update, context)
    if d.startswith("sub_buy_"):
        return await buy_subscription(update, context, int(d.split("_")[-1]))
    if d.startswith("sub_choose_"):
        return await choose_subscription_bot(update, context, int(d.split("_")[-1]))
    if d == "bot_add":
        return await add_bot_start(update, context)
    if d == "bot_list":
        return await bot_list(update, context)
    if d.startswith("bot_manage_"):
        return await bot_manage(update, context, int(d.split("_")[-1]))
    if d.startswith("bot_settings_"):
        return await bot_settings(update, context, int(d.split("_")[-1]))
    if d.startswith("bot_markup_"):
        return await bot_markup_start(update, context, int(d.split("_")[-1]))
    if d.startswith("bot_start_"):
        return await bot_start_manual(update, context, int(d.split("_")[-1]))
    if d.startswith("bot_stop_"):
        return await bot_stop_manual(update, context, int(d.split("_")[-1]))
    if d == "adm_bots":
        return await admin_bots(update, context)
    if d.startswith("adm_bot_") and d.count("_")==2:
        return await admin_bot_detail(update, context, int(d.split("_")[-1]))
    if d.startswith("adm_bot_start_"):
        bot_id=int(d.split("_")[-1]); r=child_bot_row(bot_id)
        if q.from_user.id==ADMIN_ID and r and child_active(r): await start_child_bot(bot_id)
        return await q.message.reply_text("▶️ Bot ishga tushirildi.")
    if d.startswith("adm_bot_stop_"):
        bot_id=int(d.split("_")[-1])
        if q.from_user.id==ADMIN_ID: await stop_child_bot(bot_id)
        return await q.message.reply_text("⛔ Bot to'xtatildi.")
    if d == "child_markup":
        r=child_bot_row(context.bot.id); context.user_data["state"]="bot_markup"; context.user_data["settings_bot_id"]=context.bot.id
        return await q.message.reply_text(f"💰 Hozirgi ustama: {r['markup_uzs']:,.0f} so'm\nYangi UZS summani yuboring:") if r else None
    if d == "child_settings":
        r=child_bot_row(context.bot.id)
        return await q.message.reply_text(f"⚙️ Bot sozlamalari\n\n💰 Ustama: {r['markup_uzs']:,.0f} so'm") if r else None
    if d == "child_stats":
        orders,turn=child_turnover(context.bot.id)
        return await q.message.reply_text(f"📊 Statistika\n\n📦 Buyurtmalar: {orders}\n💰 Aylanma: {turn:,.0f} so'm")
    if d == "back_home":
        context.user_data.clear()
        return await q.message.edit_text(
            "Assalomu alaykum! Donuz botiga xush kelibsiz.",
            reply_markup=main_menu()
        )

    ensure_user(
        q.from_user
    )

    try:

        if d == "games":

            await games(
                update,
                context
            )

        # Eski callback saqlangan.
        # Asosiy menyuda endi Balans tugmasi yo'q.
        elif d == "balance":

            await balance_cb(
                update,
                context
            )

        elif d == "deposit":

            await deposit(
                update,
                context
            )

        elif d == "orders":

            await orders_cb(
                update,
                context
            )

        elif d == "profile":

            await profile(
                update,
                context
            )

        elif d == "promo":

            await promo_cb(
                update,
                context
            )

        elif d.startswith("g:"):

            await game(
                update,
                context
            )

        elif d.startswith("o:"):

            await offer(
                update,
                context
            )

        elif d == "confirm":

            await confirm(
                update,
                context
            )

        elif d == "cancel":

            await cancel(
                update,
                context
            )

        elif (
            d.startswith("payok:")
            or
            d.startswith("payno:")
        ):

            await payment_action(
                update,
                context
            )

        elif d.startswith("rating:"):

            await rating_callback(
                update,
                context
            )

        elif d.startswith("price:"):

            await price_callback(
                update,
                context
            )

        elif (
            d.startswith("adm_")
            or
            d == "a_sync"
        ):

            await admin_callback(
                update,
                context
            )

    except Exception as e:

        log.exception(
            "Callback xato"
        )

        try:

            await q.message.reply_text(
                f"❌ Xatolik:\n{e}"
            )

        except Exception:

            pass


# ============================================================
# MEDIA ROUTER
# ============================================================

async def media_router(update, context):

    set_request_db(context)
    if not await child_platform_access(update, context):
        return

    if update.effective_user.id == bot_admin_id(context):

        if (
            context.user_data.get(
                "admin_state"
            )
            ==
            "post_wait_media"
        ):

            await admin_media_handler(
                update,
                context
            )

            return

    if update.message.photo:

        await photo_handler(
            update,
            context
        )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update, context):

    set_request_db(context)
    if not await child_platform_access(update, context):
        return

    if update.effective_user.id == bot_admin_id(context):

        handled = await admin_text_handler(
            update,
            context
        )

        if handled:

            return

    await text_handler(
        update,
        context
    )


# ============================================================
# CANCEL COMMAND
# ============================================================

async def cancel_command(update, context):

    set_request_db(context)
    context.user_data.clear()

    await update.message.reply_text(
        "❌ Bekor qilindi.",
        reply_markup=(
            admin_kb()
            if update.effective_user.id == ADMIN_ID
            else main_menu()
        )
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    log.exception(
        "Unhandled exception:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # RENDER PORT SERVER
    # --------------------------------------------------------

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    CURRENT_DB.set(MAIN_DB)
    init_db()
    ensure_external_schema()

    ensure_pubg()
    ensure_mobile_legends()

    # --------------------------------------------------------
    # ENV TEKSHIRISH
    # --------------------------------------------------------

    if not BOT_TOKEN:

        raise SystemExit(
            "❌ BOT_TOKEN Environment Variable yozilmagan."
        )

    if not ADMIN_ID:

        raise SystemExit(
            "❌ ADMIN_ID Environment Variable yozilmagan."
        )

    if not PLAYPAY_API_KEY:

        raise SystemExit(
            "❌ PLAYPAY_API_KEY Environment Variable yozilmagan."
        )

    if Fernet is None:
        raise SystemExit("❌ cryptography o'rnatilmagan. requirements.txt ga cryptography qo'shing.")

    # --------------------------------------------------------
    # TELEGRAM APP
    # --------------------------------------------------------

    global MAIN_BOT_ID, MAIN_BOT_USERNAME
    app = build_application(BOT_TOKEN, child=False)
    # Main bot identity
    async def _register_main():
        global MAIN_BOT_ID, MAIN_BOT_USERNAME
        me = await app.bot.get_me()
        MAIN_BOT_ID = me.id
        MAIN_BOT_USERNAME = me.username or ""
    # run_polling will initialize later, so fetch identity through a short temp request
    import requests as _requests
    rr = _requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=20)
    if not rr.ok or not rr.json().get("ok"):
        raise SystemExit("❌ BOT_TOKEN ishlamaydi.")
    me = rr.json()["result"]
    MAIN_BOT_ID = int(me["id"])
    MAIN_BOT_USERNAME = me.get("username", "")

    # Existing child bots are started by the job queue after polling starts.

    # --------------------------------------------------------
    # LOG
    # --------------------------------------------------------

    print(
        "=============================="
    )

    print(
        "       PLAYPAY DONAT BOT"
    )

    print(
        "       BOT ISHLAYAPTI"
    )

    print(
        f"       HTTP PORT: {PORT}"
    )

    print(
        "       PUBG GAME ID: 141"
    )

    print(
        "       MOBILE LEGENDS ID: 54"
    )

    print(
        "       MARKUP: 0%"
    )

    print(
        "       CATALOG AUTO SYNC: OFF"
    )

    print(
        "=============================="
    )

    # --------------------------------------------------------
    # TELEGRAM POLLING
    # --------------------------------------------------------

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()

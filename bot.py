import sqlite3
import re
import os

# PostgreSQL (Supabase) uchun moslash qatlami. Child botlar alohida SQLite faylida qoladi.
try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None
    DictCursor = None

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

class PGCursor:
    def __init__(self, cur, last_id=None):
        self._cur = cur
        self.lastrowid = last_id
    def fetchone(self): return self._cur.fetchone()
    def fetchall(self): return self._cur.fetchall()
    def __iter__(self): return iter(self._cur)
    @property
    def rowcount(self): return self._cur.rowcount

class PGConnection:
    def __init__(self, url):
        if not psycopg2:
            raise RuntimeError("Render Requirements ga psycopg2-binary qo'shing")
        self._c = psycopg2.connect(url, cursor_factory=DictCursor, connect_timeout=20)
        self._last_id = None
    @property
    def row_factory(self): return None
    @row_factory.setter
    def row_factory(self, value): pass
    def _sql(self, sql):
        q = sql.strip()
        if q.upper() == "SELECT LAST_INSERT_ROWID()":
            return "SELECT %s AS last_insert_rowid", ("last_insert",)
        if q.upper() == "BEGIN IMMEDIATE":
            return "BEGIN", False
        if q.upper().startswith("PRAGMA JOURNAL_MODE") or q.upper().startswith("PRAGMA FOREIGN_KEYS"):
            return "SELECT 1", False
        m = re.match(r"PRAGMA\s+table_info\(([^)]+)\)", q, re.I)
        if m:
            table=m.group(1).strip('"` ')
            return ("SELECT ordinal_position AS cid, column_name AS name, data_type AS type, "
                    "CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END AS notnull, "
                    "column_default AS dflt_value, 0 AS pk FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", ("table_info", table))
        q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", q, flags=re.I)
        ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE", sql, re.I))
        replace = bool(re.search(r"INSERT\s+OR\s+REPLACE", sql, re.I))
        q = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", q, flags=re.I)
        if re.match(r"(CREATE|ALTER)\s+", q, re.I):
            q = pg_schema(q)
        q = q.replace("?", "%s")
        if ignore and "ON CONFLICT" not in q.upper():
            q += " ON CONFLICT DO NOTHING"
        elif replace and "ON CONFLICT" not in q.upper():
            mcols = re.search(r'INSERT\s+INTO\s+([\w\"]+)\s*\(([^)]+)\)', q, re.I)
            if mcols:
                cols=[x.strip().strip('"') for x in mcols.group(2).split(',')]
                # For replace semantics, update every supplied column except common key columns.
                keys={'id','user_id','game_id','paket_id','key','code','token','bot_id','country_code','invited_user_id','payment_id'}
                upd=[x for x in cols if x.lower() not in keys]
                if upd:
                    q += " ON CONFLICT DO UPDATE SET " + ",".join(f'"{x}"=EXCLUDED."{x}"' for x in upd)
                else:
                    q += " ON CONFLICT DO NOTHING"
        return q, False
    def execute(self, sql, params=()):
        query, metadata = self._sql(sql)
        cur=self._c.cursor()
        if isinstance(metadata, tuple) and metadata and metadata[0] == "last_insert":
            cur.execute(query, (self._last_id,))
            return PGCursor(cur)
        if isinstance(metadata, tuple) and metadata and metadata[0] == "table_info":
            cur.execute(query, (metadata[1],))
            return PGCursor(cur)
        # SQLite INSERT OR REPLACE uchun PostgreSQL'da PK/unique target shart.
        # Avval eski noto'g'ri ON CONFLICT DO UPDATE qismini topib, jadval PK sini aniqlaymiz.
        replace_match = re.search(r"\s+ON\s+CONFLICT\s+DO\s+UPDATE\s+SET\s+(.+)$", query, re.I | re.S)
        if replace_match:
            update_clause = replace_match.group(1).strip()
            base_query = query[:replace_match.start()].rstrip()
            table_match_tmp = re.match(r'\s*INSERT\s+INTO\s+([\w"]+)', base_query, re.I)
            if table_match_tmp:
                table_name = table_match_tmp.group(1).strip('"').split('.')[-1]
                pk_cur = self._c.cursor()
                pk_cur.execute("""
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_class t ON t.oid = i.indrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
                    WHERE i.indisprimary AND n.nspname = 'public' AND t.relname = %s
                    ORDER BY array_position(i.indkey, a.attnum)
                """, (table_name,))
                pk_cols = [row[0] for row in pk_cur.fetchall()]
                pk_cur.close()
                if pk_cols:
                    target = ", ".join('"' + col.replace('"', '""') + '"' for col in pk_cols)
                    query = base_query + " ON CONFLICT (" + target + ") DO UPDATE SET " + update_clause
                else:
                    query = base_query + " ON CONFLICT DO NOTHING"
            else:
                query = base_query + " ON CONFLICT DO NOTHING"
        # sqlite lastrowid replacement: ask PostgreSQL to return generated id.
        is_insert = bool(re.match(r'\s*INSERT\s+INTO\s+([\w"]+)', query, re.I))
        table_match = re.match(r'\s*INSERT\s+INTO\s+([\w"]+)', query, re.I)
        has_returning = " RETURNING " in query.upper()
        if is_insert and table_match and not has_returning and not re.search(r"ON CONFLICT DO NOTHING\s*$", query, re.I):
            table=table_match.group(1).strip('"')
            # Tables with generated integer ids only; other inserts need no lastrowid.
            if table.lower() in {'payments','orders','withdrawals','balance_history','omonats','external_orders','web_auth_codes','web_orders'}:
                query += " RETURNING id"
        cur.execute(query, params or ())
        last_id=None
        if " RETURNING id" in query.upper():
            row=cur.fetchone()
            if row:
                last_id=row['id']
        if last_id is not None:
            self._last_id = last_id
        return PGCursor(cur, last_id)
    def rollback(self): self._c.rollback()
    def executescript(self, script):
        # init_db schema statements are simple CREATE TABLE/INDEX statements.
        cur=self._c.cursor()
        for stmt in script.split(';'):
            stmt=stmt.strip()
            if stmt: cur.execute(pg_schema(stmt))
        return PGCursor(cur)
    def commit(self): self._c.commit()
    def close(self): self._c.close()


def pg_schema(sql):
    sql=re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    sql=re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.I)
    sql=re.sub(r"\bINTEGER\b", "BIGINT", sql, flags=re.I)
    return sql

def db_connect(path=None, timeout=30):
    # Asosiy DB Supabase PostgreSQL; child bot DBlari avvalgidek SQLite.
    if path == MAIN_DB or path is None:
        if not DATABASE_URL:
            raise RuntimeError("Render Environment'da DATABASE_URL yo'q")
        return PGConnection(DATABASE_URL)
    c=sqlite3.connect(path, timeout=timeout)
    c.row_factory=sqlite3.Row
    return c
import logging
import uuid
import json
import os
import asyncio
import threading
import contextvars
import hashlib
import hmac
import base64
import html
import re
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:
    Fernet = None
    InvalidToken = Exception

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

BAY2GAME_API_KEY = os.getenv("BAY2GAME_API_KEY", "").strip()
PLAYPAY_API_KEY = os.getenv("PLAYPAY_API_KEY", "").strip()
PLAYPAY_BASE = os.getenv("PLAYPAY_BASE", "https://playpay.uz/api/v1").rstrip("/")
ORDER_CHANNEL_ID = os.getenv("ORDER_CHANNEL_ID", "@Donuz_Buyurtmlar").strip() or "@Donuz_Buyurtmlar"
BAY2GAME_BASE = os.getenv("BAY2GAME_BASE", "https://api.bay2game.xyz/api").rstrip("/")
# YuPay faqat Roblox katalogi/buyurtmalari uchun ishlatiladi.
YUPAY_BASE = os.getenv("YUPAY_BASE", "https://api.yupay.uz").rstrip("/")
YUPAY_API_KEY = os.getenv("YUPAY_API_KEY", "").strip()  # ypk_... Bearer kaliti
YUPAY_KEY_ID = os.getenv("YUPAY_KEY_ID", "").strip()    # ypm_... HMAC kalit ID
YUPAY_SECRET = os.getenv("YUPAY_SECRET", "").strip()    # ypms_... HMAC maxfiy kalit
try:
    YUPAY_USD_UZS = Decimal(os.getenv("YUPAY_USD_UZS", os.getenv("BAY2GAME_USD_UZS", "13000")))
except Exception:
    YUPAY_USD_UZS = Decimal("13000")
# Bay2Game narxlari USD da. Kurs Render ENV orqali beriladi; admin ustamasi bot ichida boshqariladi.
try:
    BAY2GAME_USD_UZS = Decimal(os.getenv("BAY2GAME_USD_UZS", "13000"))
except Exception:
    BAY2GAME_USD_UZS = Decimal("13000")

# Eski maxsus IDlar (Grand Mobile narxlari o'zgarmaydi)
PUBG_GAME_ID = 141
PUBG_GLOBAL_GAME_ID = 142  # PUBG Mobile Global, alohida API katalogi bo'lsa
MOBILE_LEGENDS_GAME_ID = 54
MOBILE_LEGENDS_GLOBAL_GAME_ID = 143
GRAND_MOBILE_GAME_ID = 999001
ROBLOX_GAME_ID = 999002

# Grand Mobile — faqat manual buyurtma, narxlar o'zgartirilmaydi
GRAND_MOBILE_PRICES = {
    15: 3000,
    30: 5500,
    90: 15500,
    150: 26000,
    200: 34000,
    300: 53000,
    400: 71500,
    500: 85000,
    1000: 169000,
    1500: 258000,
    2000: 345000,
    2500: 422000,
    3000: 519000,
}

# 0 = Bay2Game API narxiga ustama qo'shilmaydi
DEFAULT_MARKUP = Decimal("0")

# Balans to'ldirish kartasi
PAYMENT_CARD = os.getenv("PAYMENT_CARD", "").strip()

# Telegram Stars / Premium ham Bay2Game katalogi va buyurtma API orqali ishlaydi.
TELEGRAM_MARKUP_PERCENT = Decimal("4.5")

# SIM ustamasi sozlamasi
SIM_MARKUP_PERCENT = Decimal(os.getenv("SIM_MARKUP_PERCENT", "35"))

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
    c = db_connect(MAIN_DB, timeout=30)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT db_path FROM child_bots WHERE bot_id=?", (int(bot_id),)).fetchone()
    c.close()
    path = row["db_path"] if row and row["db_path"] else MAIN_DB
    CURRENT_DB.set(path)
    return path


def main_conn():
    c = db_connect(MAIN_DB, timeout=30)
    c.row_factory = sqlite3.Row
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


async def notify_order_channel(context, message):
    """Har bir bot buyurtmasini asosiy bot orqali Donuz kanaliga yuboradi."""
    try:
        return await asyncio.to_thread(web_notify_order_channel, message)
    except Exception as exc:
        log.error("Buyurtma kanaliga xabar yuborilmadi (%s): %s", ORDER_CHANNEL_ID, type(exc).__name__)
        return False


def web_notify_order_channel(message):
    """Sayt buyurtmalari uchun kanalga xabar yuborish."""
    if not BOT_TOKEN:
        log.error("Buyurtma kanaliga xabar yuborilmadi: BOT_TOKEN yo'q")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ORDER_CHANNEL_ID, "text": message},
            timeout=20,
        )
        data = response.json() if response.content else {}
        if not response.ok or not data.get("ok"):
            log.error("Kanal xabari Telegram tomonidan rad etildi: %s", data.get("description", response.status_code))
            return False
        return True
    except Exception as exc:
        log.exception("Sayt buyurtmasini kanalga yuborishda xato: %s", type(exc).__name__)
        return False



# ============================================================
# WEB SAYT API
# ============================================================

WEB_SESSION_HOURS = 2
WEB_AUTH_MINUTES = 10

def _json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _web_send_telegram_message(user_id, text):
    if not BOT_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": int(user_id), "text": text},
            timeout=15,
        )
        return bool(r.ok and r.json().get("ok"))
    except Exception:
        log.exception("Web auth Telegram xatosi")
        return False


def _web_cleanup():
    now = datetime.now().isoformat()
    c = main_conn()
    c.execute("DELETE FROM web_auth_codes WHERE expires_at < ? OR used=1", (now,))
    c.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
    c.commit()
    c.close()


def web_request_auth_code(user_id):
    _web_cleanup()
    c = main_conn()
    user = c.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (int(user_id),)
    ).fetchone()
    if not user:
        c.close()
        return False, "Telegram botda /start bosing, keyin qayta urinib ko'ring."

    code = str(uuid.uuid4().int)[-6:]
    expires = datetime.now() + timedelta(minutes=WEB_AUTH_MINUTES)

    c.execute(
        "INSERT INTO web_auth_codes(user_id,code,expires_at,used,created_at) VALUES(?,?,?,?,?)",
        (int(user_id), code, expires.isoformat(), 0, datetime.now().isoformat())
    )
    c.commit()
    c.close()

    sent = _web_send_telegram_message(
        user_id,
        "🌐 GAME DONAT saytiga kirish kodi:\n\n"
        f"🔐 {code}\n\n"
        f"⏱ Kod {WEB_AUTH_MINUTES} daqiqa amal qiladi.\n"
        "Bu kodni hech kimga bermang."
    )
    if not sent:
        return False, "Telegram xabarini yuborib bo'lmadi."
    return True, "Tasdiqlash kodi Telegram botingizga yuborildi."


def web_verify_auth_code(user_id, code):
    _web_cleanup()
    c = main_conn()
    row = c.execute(
        """
        SELECT id FROM web_auth_codes
        WHERE user_id=? AND code=? AND used=0 AND expires_at>=?
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), str(code).strip(), datetime.now().isoformat())
    ).fetchone()
    if not row:
        c.close()
        return None

    c.execute("UPDATE web_auth_codes SET used=1 WHERE id=?", (row["id"],))
    token = uuid.uuid4().hex + uuid.uuid4().hex
    expires = datetime.now() + timedelta(hours=WEB_SESSION_HOURS)
    c.execute(
        "INSERT INTO web_sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
        (token, int(user_id), expires.isoformat(), datetime.now().isoformat())
    )
    c.commit()
    c.close()
    return token


def web_session_user(token):
    if not token:
        return None
    _web_cleanup()
    c = main_conn()
    row = c.execute(
        "SELECT user_id FROM web_sessions WHERE token=? AND expires_at>=?",
        (str(token), datetime.now().isoformat())
    ).fetchone()
    c.close()
    return int(row["user_id"]) if row else None


def web_catalog():
    c = main_conn()
    games = c.execute(
        "SELECT game_id,name,id_label,requires_server,amount_based "
        "FROM games WHERE active=1 ORDER BY game_id"
    ).fetchall()
    result = []

    for g in games:
        products = c.execute(
            """
            SELECT paket_id,package_name,api_price_uzs,sale_price
            FROM products
            WHERE game_id=? AND active=1
            ORDER BY paket_id
            """,
            (g["game_id"],)
        ).fetchall()

        result.append({
            "game_id": int(g["game_id"]),
            "name": g["name"],
            "id_label": g["id_label"],
            "requires_server": bool(g["requires_server"]),
            "amount_based": bool(g["amount_based"]),
            "packages": [
                {
                    "paket_id": int(p["paket_id"]),
                    "name": p["package_name"],
                    "api_price_uzs": float(p["api_price_uzs"] or 0),
                    "price": float(p["sale_price"] or 0),
                }
                for p in products
            ],
        })

    c.close()

    # Grand Mobile manual katalogi
    result.append({
        "game_id": GRAND_MOBILE_GAME_ID,
        "name": "Grand Mobile",
        "id_label": "Grand Mobile ID",
        "requires_server": False,
        "amount_based": False,
        "manual": True,
        "packages": [
            {
                "paket_id": int(gc),
                "name": f"{gc} GC",
                "api_price_uzs": float(price),
                "price": float(price),
            }
            for gc, price in GRAND_MOBILE_PRICES.items()
        ],
    })
    return result


def web_check_id(payload):
    game_id = int(payload.get("game_id", 0))
    player_id = str(payload.get("player_id", "")).strip()
    server_id = str(payload.get("server_id", "")).strip()
    charname = str(payload.get("charname", "")).strip()

    if not player_id:
        return {"ok": False, "error": "ID kiritilmagan."}

    if game_id == GRAND_MOBILE_GAME_ID:
        return {
            "ok": True,
            "manual": True,
            "player_id": player_id,
            "message": "Grand Mobile buyurtmasi manual tekshiriladi."
        }

    status, data = playpay_check_id(
        game_id, player_id,
        server_id=server_id or None,
        charname=charname or None
    )

    if status and isinstance(data, dict):
        data["http_status"] = status
        return data

    return {"ok": False, "error": "ID tekshirishda xatolik."}


def web_create_order(payload, user_id):
    """Sayt buyurtmasi: buyurtma tarixi DBga yozilmaydi, xabar mijoz va kanalga boradi."""
    try:
        game_id = int(payload.get("game_id", 0))
        paket_id = int(payload.get("paket_id", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "O'yin yoki paket noto'g'ri."}

    player_id = str(payload.get("player_id", "")).strip()
    server_id = str(payload.get("server_id", "")).strip()
    if not player_id:
        return {"ok": False, "error": "ID kiritilmagan."}

    c = main_conn()
    p = c.execute(
        "SELECT * FROM products WHERE game_id=? AND paket_id=? AND active=1",
        (game_id, paket_id)
    ).fetchone()
    c.close()

    if game_id == GRAND_MOBILE_GAME_ID:
        if paket_id not in GRAND_MOBILE_PRICES:
            return {"ok": False, "error": "Grand Mobile paketi topilmadi."}
        price = Decimal(str(GRAND_MOBILE_PRICES[paket_id]))
        product_name = f"Grand Mobile - {paket_id} GC"
    else:
        if not p:
            return {"ok": False, "error": "Paket topilmadi."}
        price = Decimal(str(p["sale_price"] or 0))
        product_name = p["package_name"]

    if price <= 0:
        return {"ok": False, "error": "Narx noto'g'ri."}

    balance = Decimal(str(get_balance(user_id)))
    if balance < price:
        return {"ok": False, "error": "Balans yetarli emas.", "balance": float(balance), "required": float(price)}

    if game_id != GRAND_MOBILE_GAME_ID:
        check = web_check_id({"game_id": game_id, "player_id": player_id, "server_id": server_id})
        if not check.get("ok"):
            return {"ok": False, "error": "ID tekshirilmadi.", "details": check}

    # Mablag'ni yechish; order ma'lumotlari alohida jadvalga yozilmaydi.
    add_balance(user_id, -price, "order_hold", f"Sayt buyurtmasi: {product_name}")
    now_text = datetime.now(ZoneInfo("Asia/Tashkent")).strftime("%d.%m.%Y %H:%M:%S")

    if game_id == GRAND_MOBILE_GAME_ID:
        notice = (
            f"🚗 YANGI GRAND MOBILE BUYURTMA\n\n"
            f"📦 {product_name}\n🆔 ID: {player_id}\n"
            f"💰 {price:,.0f} so'm\n👤 Telegram ID: {user_id}\n"
            f"🕒 {now_text}\n📊 Holat: Manual tekshiruvda"
        )
        sent_channel = web_notify_order_channel(notice)
        sent_user = _web_send_telegram_message(
            user_id,
            "✅ Buyurtmangiz qabul qilindi!\n\n" + notice
        )
        if not sent_channel or not sent_user:
            log.error("Grand Mobile web buyurtma xabari to'liq yuborilmadi: channel=%s user=%s", sent_channel, sent_user)
        return {"ok": True, "status": "manual_pending", "message": "Buyurtma qabul qilindi. Telegram xabaringizni tekshiring."}

    body = {"game_id": game_id, "paket_id": paket_id, "player_id": player_id}
    if server_id:
        body["server_id"] = server_id
    status, data = api_post("/order", body)

    if not isinstance(data, dict) or not data.get("ok"):
        add_balance(user_id, price, "order_refund", f"Sayt buyurtmasi bekor qilindi: {str(data)[:200]}")
        return {"ok": False, "error": "Buyurtmani bajarib bo'lmadi. Pul balansingizga qaytarildi."}

    provider_order_id = str(data.get("order_id", ""))
    order_status = str(data.get("status", "processing"))
    notice = (
        f"🛒 YANGI BUYURTMA\n\n📦 {product_name}\n"
        f"🆔 O'yinchi ID: {player_id}\n"
        + (f"🌐 Server ID: {server_id}\n" if server_id else "")
        + f"💰 Summa: {price:,.0f} so'm\n👤 Telegram ID: {user_id}\n"
        f"🕒 {now_text}\n🔢 Provayder ID: {provider_order_id or '-'}\n📊 Holat: {order_status}"
    )
    sent_channel = web_notify_order_channel(notice)
    sent_user = _web_send_telegram_message(user_id, "✅ Buyurtmangiz qabul qilindi!\n\n" + notice)
    if not sent_channel or not sent_user:
        log.error("Web buyurtma xabari to'liq yuborilmadi: channel=%s user=%s", sent_channel, sent_user)

    return {"ok": True, "status": order_status, "provider_order_id": provider_order_id,
            "message": "Buyurtma qabul qilindi. Telegram xabaringizni tekshiring."}


class WebAPIHandler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _reply(self, data, status=200):
        raw = _json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _auth_token(self):
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return ""

    def do_HEAD(self):
        from urllib.parse import urlparse
        if urlparse(self.path).path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse

        path = urlparse(self.path).path

        try:
            if path == "/" or path == "/health":
                return self._reply({
                    "ok": True,
                    "service": "GAME DONAT",
                    "status": "online",
                })

            if path == "/api/catalog":
                return self._reply({"ok": True, "games": web_catalog()})

            if path == "/api/me":
                uid = web_session_user(self._auth_token())
                if not uid:
                    return self._reply({"ok": False, "error": "Kirish talab qilinadi."}, 401)
                return self._reply({
                    "ok": True,
                    "user_id": uid,
                    "balance": float(get_balance(uid)),
                })

            return self._reply({"ok": False, "error": "Endpoint topilmadi."}, 404)

        except Exception:
            log.exception("Web GET API xatosi")
            return self._reply({"ok": False, "error": "Server xatosi."}, 500)

    def do_POST(self):
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        payload = self._body()

        try:
            if path == "/api/auth/request":
                try:
                    uid = int(payload.get("telegram_id", 0))
                except Exception:
                    uid = 0
                if not uid:
                    return self._reply({"ok": False, "error": "Telegram ID noto'g'ri."}, 400)
                ok, msg = web_request_auth_code(uid)
                return self._reply({"ok": ok, "message": msg}, 200 if ok else 400)

            if path == "/api/auth/verify":
                try:
                    uid = int(payload.get("telegram_id", 0))
                except Exception:
                    uid = 0
                token = web_verify_auth_code(uid, payload.get("code", ""))
                if not token:
                    return self._reply({"ok": False, "error": "Kod noto'g'ri yoki muddati tugagan."}, 401)
                return self._reply({
                    "ok": True,
                    "token": token,
                    "user_id": uid,
                    "balance": float(get_balance(uid)),
                })

            if path == "/api/check-id":
                result = web_check_id(payload)
                return self._reply(result, 200 if result.get("ok") else 400)

            if path == "/api/order":
                uid = web_session_user(self._auth_token())
                if not uid:
                    return self._reply({"ok": False, "error": "Avval Telegram orqali kiring."}, 401)
                result = web_create_order(payload, uid)
                return self._reply(result, 200 if result.get("ok") else 400)

            return self._reply({"ok": False, "error": "Endpoint topilmadi."}, 404)

        except Exception:
            log.exception("Web POST API xatosi")
            return self._reply({"ok": False, "error": "Server xatosi."}, 500)


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(WebAPIHandler):
    pass


def start_health_server():

    try:
        server = ThreadingHTTPServer(
            ("0.0.0.0", PORT),
            WebAPIHandler
        )

        log.info(
            "Render Web/API server ishga tushdi: 0.0.0.0:%s",
            PORT
        )

        server.serve_forever()

    except Exception:
        log.exception("HTTP Web/API server xatosi")


# ============================================================
# DATABASE
# ============================================================

def conn():

    c = db_connect(active_db(), timeout=30)

    c.row_factory = sqlite3.Row

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
        amount REAL DEFAULT 0,
        requested_amount REAL DEFAULT 0,
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
        bot_id INTEGER NOT NULL DEFAULT 0,
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

    CREATE TABLE IF NOT EXISTS main_bot_users(
        user_id INTEGER PRIMARY KEY,
        first_started_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS referrals(
        invited_user_id INTEGER PRIMARY KEY,
        referrer_user_id INTEGER NOT NULL,
        bonus REAL NOT NULL DEFAULT 100,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_details TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        decided_at TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS balance_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        note TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS omonats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        principal REAL NOT NULL,
        profit REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        started_at TEXT NOT NULL,
        matures_at TEXT NOT NULL,
        closed_at TEXT DEFAULT '',
        paid_at TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_omonats_due ON omonats(status, matures_at);
    CREATE INDEX IF NOT EXISTS idx_omonats_user ON omonats(user_id, id);

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
    """)

    # Eski bazalarda payments jadvali turlicha bo'lishi mumkin.
    # Kerakli ustunlarni qo'shamiz; eski majburiy amount ustunini saqlab qolamiz.
    payment_columns = {
        r["name"]
        for r in c.execute("PRAGMA table_info(payments)").fetchall()
    }
    payment_migrations = {
        # Eski bazalarda amount ustuni NOT NULL bo'lishi mumkin.
        # Yangi va eski sxemalarni bir xil ishlashi uchun ikkala summa ustuni saqlanadi.
        "amount": "REAL DEFAULT 0",
        "requested_amount": "REAL DEFAULT 0",
        "approved_amount": "REAL DEFAULT 0",
        "photo_id": "TEXT DEFAULT ''",
        "status": "TEXT DEFAULT 'pending'",
        "created_at": "TEXT DEFAULT ''",
        "approved_at": "TEXT DEFAULT ''",
    }
    for column, definition in payment_migrations.items():
        if column not in payment_columns:
            c.execute(
                f"ALTER TABLE payments ADD COLUMN {column} {definition}"
            )
    c.commit()
    c.close()

    set_default(
        "payment_card",
        PAYMENT_CARD
    )
    set_default("donat_markup_percent", DEFAULT_MARKUP)
    set_default("roblox_markup_percent", DEFAULT_MARKUP)
    set_default("bay2game_usd_uzs", os.getenv("BAY2GAME_USD_UZS", "13000"))
    set_default("stars_markup_percent", TELEGRAM_MARKUP_PERCENT)
    set_default("stars_unit_price_uzs", "0")
    set_default("premium_markup_percent", TELEGRAM_MARKUP_PERCENT)
    set_default("sim_markup_percent", SIM_MARKUP_PERCENT)
    set_default("referral_bonus_uzs", "100")

    if active_db() == MAIN_DB:
        c = conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS custom_prices(
                country_code TEXT PRIMARY KEY,
                custom_price REAL NOT NULL
            )
        """)
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
            # Older PostgreSQL databases may have an orders table created by a previous version.
            # Add every column used by current order flows before handlers start.
            "bot_id": "BIGINT NOT NULL DEFAULT 0",
            "user_id": "BIGINT",
            "playpay_order_id": "TEXT DEFAULT ''",
            "game_id": "BIGINT DEFAULT 0",
            "paket_id": "BIGINT DEFAULT 0",
            "product_name": "TEXT DEFAULT ''",
            "player_id": "TEXT DEFAULT ''",
            "fields_json": "TEXT DEFAULT '{}'",
            "cost_usd": "DOUBLE PRECISION DEFAULT 0",
            "charged_usd": "DOUBLE PRECISION DEFAULT 0",
            "sale_price": "DOUBLE PRECISION DEFAULT 0",
            "status": "TEXT DEFAULT 'pending'",
            "created_at": "TEXT DEFAULT ''",
            "updated_at": "TEXT DEFAULT ''",
            "notified": "TEXT DEFAULT '0'",
            "provider": "TEXT DEFAULT 'bay2game'",
            "service_type": "TEXT DEFAULT ''",
            "target": "TEXT DEFAULT ''",
            "quantity": "DOUBLE PRECISION DEFAULT 0",
            "months": "BIGINT DEFAULT 0",
            "provider_order_id": "TEXT DEFAULT ''",
        }
        for name, typ in additions.items():
            if name not in existing:
                c.execute(f"ALTER TABLE orders ADD COLUMN {name} {typ}")

        # Bay2Game uses string game_code/product_code; map them to internal numeric IDs.
        for table, cols in {
            "games": {
                "provider_game_code": "TEXT DEFAULT ''",
                "game_fields_json": "TEXT DEFAULT '[]'",
                "provider": "TEXT DEFAULT 'bay2game'",
            },
            "products": {
                "provider_product_code": "TEXT DEFAULT ''",
                "provider": "TEXT DEFAULT 'bay2game'",
                "provider_group": "TEXT DEFAULT ''",
                "provider_required_fields_json": "TEXT DEFAULT '[]'",
                "provider_sku_kind": "TEXT DEFAULT 'fixed'",
                "provider_price_usd": "TEXT DEFAULT ''",
            },
        }.items():
            present = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols.items():
                if col not in present:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

        c.execute("""
            CREATE TABLE IF NOT EXISTS provider_refunds(
                provider_order_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

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

        # Web sayt uchun xavfsiz Telegram tasdiqlash
        c.execute("""
            CREATE TABLE IF NOT EXISTS web_auth_codes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS web_sessions(
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS web_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                user_id INTEGER,
                game_id INTEGER,
                paket_id INTEGER,
                product_name TEXT,
                player_id TEXT,
                server_id TEXT DEFAULT '',
                price REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                playpay_order_id TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        c.commit()
    finally:
        c.close()


# ============================================================
# BOT PLATFORM
# ============================================================

def token_cipher():
    if Fernet is None:
        raise RuntimeError("cryptography o'rnatilmagan. requirements.txt ga cryptography qo'shing.")
    # Eski Render sozlamalarida ENCRYPTION_KEY nomi ishlatilgan.
    # Yangi nom ham qo'llab-quvvatlanadi; mavjud kalitni almashtirmang.
    raw = (
        os.getenv("BOT_TOKEN_ENCRYPTION_KEY", "").strip()
        or os.getenv("ENCRYPTION_KEY", "").strip()
    )
    if not raw:
        # Faqat ilgari ham shu fallback bilan shifrlangan eski o'rnatmalar uchun.
        raw = base64.urlsafe_b64encode(hashlib.sha256(BOT_TOKEN.encode()).digest()).decode()
    try:
        return Fernet(raw.encode())
    except Exception as e:
        raise RuntimeError(
            "Fernet kaliti noto'g'ri. Render Environment'da "
            "ENCRYPTION_KEY yoki BOT_TOKEN_ENCRYPTION_KEY qiymatini tekshiring."
        ) from e


def encrypt_token(token):
    return token_cipher().encrypt(token.encode()).decode()


def decrypt_token(value):
    try:
        return token_cipher().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Child bot tokenini ochib bo'lmadi: ENCRYPTION_KEY avvalgi kalit bilan "
            "bir xil emas yoki token_enc buzilgan. Eski kalitni Render'da tiklang."
        ) from e


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


async def notify_bot_owner_technical(context, user_id=None, details=None):
    """Bot egasiga texnik xabar yuboradi; mijozga maxfiy API tafsilotlari ko'rsatilmaydi."""
    try:
        owner_id = int(bot_admin_id(context) or 0)
        if not owner_id or (user_id is not None and int(owner_id) == int(user_id)):
            return
        detail_text = str(details or "").strip()
        if len(detail_text) > 700:
            detail_text = detail_text[:700] + "..."
        await context.bot.send_message(
            chat_id=owner_id,
            text=(
                "⚠️ Avtomatik buyurtma bajarilmadi.\n\n"
                "Ulangan botda buyurtma API tomonidan qabul qilinmadi.\n"
                "Balans, API kaliti, katalogdagi mahsulot kodi va Render loglarini tekshiring."
                + (f"\n\n🔎 Xato tafsiloti: {detail_text}" if detail_text else "")
            )
        )
    except Exception:
        log.exception("Bot egasiga texnik xabar yuborishda xato")


def set_request_db(context):
    return set_active_db_for_bot(context.bot.id)


def child_active(row):
    # Child botlar doim faol.
    return bool(row)


def child_grace_expired(row):
    # Muddat bo'yicha o'chirish yo'q.
    return False


def child_status(row):
    if not row:
        return "deleted"
    return "active"


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
    try:
        cols = {row[1] for row in c.execute("PRAGMA table_info(orders)").fetchall()}
        if "sale_price" in cols:
            row = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(sale_price),0) FROM orders "
                "WHERE status NOT IN ('failed','cancelled','rejected')"
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*), 0 FROM orders "
                "WHERE status NOT IN ('failed','cancelled','rejected')"
            ).fetchone()
    finally:
        c.close()
    return int(row[0] or 0), float(row[1] or 0)




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
    # Eski DB ustunlari saqlanadi, lekin ular endi hech qanday cheklov bermaydi.
    trial = now
    grace = now
    db_path = str(CHILD_DIR / f"{bot_id}.db")
    CURRENT_DB.set(db_path)
    init_db(); ensure_external_schema()
    CURRENT_DB.set(MAIN_DB)
    # copy current catalog/settings into child DB
    copy_catalog_to_child(db_path)
    c = main_conn()
    c.execute("""INSERT INTO child_bots(bot_id,bot_username,bot_name,owner_user_id,owner_username,token_enc,db_path,created_at,trial_until,subscription_until,grace_until,status,markup_uzs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""", (bot_id, me.get("username", ""), me.get("first_name", ""), int(owner_id), owner_username or "", encrypt_token(token), db_path, now.isoformat(), trial.isoformat(), "", grace.isoformat(), "active"))
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
    # Child SQLite bazasida provider kodlarini ham saqlaymiz; Bay2Game buyurtmasi
    # ichki sonli ID emas, asl product_code orqali yuborilishi kerak.
    game_cols = {row[1] for row in c.execute("PRAGMA table_info(games)").fetchall()}
    product_cols = {row[1] for row in c.execute("PRAGMA table_info(products)").fetchall()}
    game_fields = ["game_id", "name", "id_label", "requires_server", "amount_based", "active", "updated_at"]
    for optional in ("provider_game_code", "game_fields_json", "provider"):
        if optional in game_cols:
            game_fields.append(optional)
    product_fields = ["game_id", "paket_id", "game_name", "package_name", "price_usd", "api_price_uzs", "sale_price", "active", "updated_at"]
    for optional in ("provider_product_code", "provider", "provider_group", "provider_required_fields_json", "provider_sku_kind", "provider_price_usd"):
        if optional in product_cols:
            product_fields.append(optional)

    game_sql = (
        f"INSERT OR REPLACE INTO games({','.join(game_fields)}) "
        f"VALUES({','.join('?' for _ in game_fields)})"
    )
    product_sql = (
        f"INSERT OR REPLACE INTO products({','.join(product_fields)}) "
        f"VALUES({','.join('?' for _ in product_fields)})"
    )
    for r in games_rows:
        c.execute(game_sql, tuple(r[x] for x in game_fields))
    for r in prod_rows:
        c.execute(product_sql, tuple(r[x] for x in product_fields))
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
    old_db = CURRENT_DB.get()
    CURRENT_DB.set(row["db_path"])
    try:
        init_db()
        ensure_external_schema()
    finally:
        CURRENT_DB.set(old_db or MAIN_DB)
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
    # Child botlar muddat sababli o'chirilmaydi.
    set_request_db(context)
    c = main_conn()
    rows = c.execute("SELECT * FROM child_bots").fetchall()
    c.close()

    for row in rows:
        bot_id = int(row["bot_id"])
        try:
            c = main_conn()
            c.execute(
                "UPDATE child_bots SET status='active' WHERE bot_id=?",
                (bot_id,)
            )
            c.commit()
            c.close()

            if bot_id not in CHILD_APPS:
                await start_child_bot(bot_id)
        except Exception as e:
            # Bitta noto'g'ri shifrlangan token scheduler'ni to'xtatmasin.
            log.error("Child bot %s start bo'lmadi: %s", bot_id, e)


def build_application(token, child=False):
    app = Application.builder().token(token).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(subscription_check_callback, pattern=r"^donuz_sub_check$"))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.ANIMATION | filters.VIDEO, media_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    if app.job_queue:
        app.job_queue.run_repeating(check_orders, interval=30, first=30)
        if not child:
            app.job_queue.run_repeating(manage_child_bots, interval=60, first=5)
    return app


async def child_platform_access(update, context):
    # Platforma cheklovi yo'q.
    return True


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
    text=(f"🤖 <b>@{r['bot_username'] or bot_id}</b>\n\n🆔 Bot ID: <code>{bot_id}</code>\n👤 Egasi ID: <code>{r['owner_user_id']}</code>\n📅 Qo'shilgan: {format_dt(r['created_at'])}\n💰 Aylanma: {turn:,.0f} so'm\n📦 Buyurtmalar: {orders}\n🟢 Holati: ♾️ Doimiy faol\n💵 Ustama: {r['markup_uzs']:,.0f} so'm")
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
    text=(f"🤖 <b>@{r['bot_username'] or bot_id}</b>\n\n🆔 Bot ID: <code>{bot_id}</code>\n👤 Egasi: @{r['owner_username'] or 'username'}\n👤 Owner ID: <code>{r['owner_user_id']}</code>\n📅 Qo'shilgan: {format_dt(r['created_at'])}\n💰 Aylanma: {turn:,.0f} so'm\n📦 Buyurtmalar: {orders}\n🟢 Holati: ♾️ Doimiy faol")
    kb=[[InlineKeyboardButton("▶️ Ishga tushirish",callback_data=f"adm_bot_start_{bot_id}"),InlineKeyboardButton("⛔ To'xtatish",callback_data=f"adm_bot_stop_{bot_id}")],[InlineKeyboardButton("🔙 Orqaga",callback_data="adm_bots")]]
    await q.message.reply_text(text,parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))


# ============================================================
# TELEGRAM STARS / PREMIUM — BAY2GAME ONLY
# ============================================================
_B2TG_TZ = ZoneInfo("Asia/Tashkent")


def b2tg_now_text():
    """Buyurtma vaqtini O'zbekiston vaqti bilan sekundigacha qaytaradi."""
    return datetime.now(_B2TG_TZ).strftime("%Y-%m-%d %H:%M:%S")


async def b2tg_admin_report(context, *, result, uid, username, game_name,
                            product_name, price, started_at, order_id="-",
                            http_status="-", api_data=None, balance_action="-"):
    """Har bir Stars/Premium buyurtmasi haqida egasiga batafsil hisobot."""
    try:
        owner_id = int(bot_admin_id(context) or 0)
        if not owner_id:
            return
        raw = json.dumps(api_data or {}, ensure_ascii=False, default=str)
        if len(raw) > 2200:
            raw = raw[:2200] + "..."
        user = "@" + str(username).lstrip("@") if username else "-"
        report = (
            f"📋 TELEGRAM BUYURTMA HISOBOTI\n\n"
            f"🕒 Vaqt (Toshkent): {started_at}\n"
            f"📌 Natija: {result}\n"
            f"📦 Xizmat: {game_name} — {product_name}\n"
            f"👤 Qabul qiluvchi: {user}\n"
            f"🆔 Telegram User ID: {uid}\n"
            f"💰 Summa: {price:,.0f} so'm\n"
            f"🧾 Buyurtma ID: {order_id}\n"
            f"🌐 API HTTP: {http_status}\n"
            f"💳 Balans amali: {balance_action}\n"
            f"🧩 API javobi: {raw}"
        )
        await context.bot.send_message(chat_id=owner_id, text=report)
    except Exception:
        log.exception("Stars/Premium buyurtma hisobotini adminga yuborib bo'lmadi")


def _b2tg_provider_status(data):
    """Javob ichidagi aniq yakuniy holatni topadi; noaniq javobni failure demaydi."""
    if not isinstance(data, dict):
        return ""
    candidates = [data]
    raw = data.get("raw")
    if isinstance(raw, dict):
        candidates.append(raw)
        nested = raw.get("data")
        if isinstance(nested, dict):
            candidates.append(nested)
            if isinstance(nested.get("order"), dict):
                candidates.append(nested["order"])
        if isinstance(raw.get("order"), dict):
            candidates.append(raw["order"])
    nested = data.get("data")
    if isinstance(nested, dict):
        candidates.append(nested)
        if isinstance(nested.get("order"), dict):
            candidates.append(nested["order"])
    for item in reversed(candidates):
        status = str(item.get("status") or "").strip().lower()
        if status:
            return status
    return ""
def bay2game_telegram_keyboard():
    # Foydalanuvchiga katalogdagi har bir kategoriya emas, faqat ikki xizmat ko'rsatiladi.
    rows = [
        [InlineKeyboardButton("⭐ Telegram Stars", callback_data="b2tg_type_stars")],
        [InlineKeyboardButton("💎 Telegram Premium", callback_data="b2tg_type_premium")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(rows)


async def _b2tg_edit_or_replace(q, text, reply_markup=None, parse_mode="HTML"):
    """Avval eski bot xabarini tahrirlaydi; imkonsiz bo'lsa o'chirib yangisini yuboradi."""
    try:
        await q.message.edit_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return
    except Exception:
        pass
    try:
        await q.message.delete()
    except Exception:
        pass
    await q.message.chat.send_message(
        text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


async def bay2game_telegram_menu(update, context):
    q = update.callback_query
    await _b2tg_edit_or_replace(
        q,
        "⭐ <b>Stars / 💎 Premium</b>\n\nKerakli xizmatni tanlang:",
        reply_markup=bay2game_telegram_keyboard(),
    )


async def bay2game_telegram_type(update, context, service_type, page=0):
    """Tanlangan Telegram xizmatining paketlarini sahifalab chiqaradi."""
    q = update.callback_query
    service_type = str(service_type or "").lower()
    if service_type not in ("stars", "premium"):
        await q.message.reply_text("❌ Xizmat turi noto'g'ri.")
        return

    c = conn()
    try:
        rows = c.execute(
            """
            SELECT g.game_id, g.name AS game_name,
                   p.paket_id, p.package_name, p.sale_price,
                   p.provider_product_code
            FROM games g
            JOIN products p ON p.game_id = g.game_id
            WHERE g.active = 1 AND p.active = 1
            ORDER BY g.name, p.paket_id
            """
        ).fetchall()
    finally:
        c.close()

    matched = []
    seen = set()
    for row in rows:
        game_name = str(row["game_name"] or "").strip()
        package_name = str(row["package_name"] or "").strip()
        product_code = str(row["provider_product_code"] or "").strip().upper()
        # Bay2Game'ning aniq mahsulot kodlari bilan ajratamiz.
        # Masalan TELEGRAM_50_Stars va TELEGRAM_Premium_12_Months.
        # Boshqa o'yinlardagi Premium Pass/Gift paketlari shu sabab chiqmaydi.
        is_stars_code = product_code.startswith("TELEGRAM_") and product_code.endswith("_STARS") and "PREMIUM" not in product_code
        is_premium_code = product_code.startswith("TELEGRAM_PREMIUM_")
        if service_type == "stars" and not is_stars_code:
            continue
        if service_type == "premium" and not is_premium_code:
            continue
        key = (int(row["game_id"]), int(row["paket_id"]))
        if key in seen:
            continue
        seen.add(key)
        matched.append(row)

    # Paketlarni nom/id bo'yicha emas, haqiqiy miqdor yoki muddat bo'yicha tartiblaymiz.
    def _b2tg_package_order(row):
        code = str(row["provider_product_code"] or "").strip().upper()
        name = str(row["package_name"] or "").strip().upper()
        if service_type == "stars":
            # TELEGRAM_50_STARS -> 50; nomidan ham zaxira tarzida sonni olamiz.
            match = re.search(r"TELEGRAM_(\d+)_STARS", code)
            if not match:
                match = re.search(r"(\d+)\s*(?:STARS|STAR)", name)
            amount = int(match.group(1)) if match else 10**12
            return (amount, name, int(row["paket_id"]))
        # TELEGRAM_Premium_12_Months -> 12; turli nomlanishlarni ham qabul qiladi.
        match = re.search(r"TELEGRAM_PREMIUM_(\d+)_MONTHS?", code)
        if not match:
            match = re.search(r"(\d+)\s*(?:MONTHS?|MOY|OY)", name)
        months = int(match.group(1)) if match else 10**12
        return (months, name, int(row["paket_id"]))

    matched.sort(key=_b2tg_package_order)
    label = "Telegram Stars" if service_type == "stars" else "Telegram Premium"
    if not matched:
        await q.message.reply_text(
            f"📦 {label} paketlari katalogda topilmadi.\n\n"
            "Katalogni yangilashni administrator tekshirishi kerak."
        )
        return

    # Har bir sahifada ko'pi bilan 8 ta mahsulot: Telegram markup limitidan oshmasin.
    page_size = 8
    total_pages = (len(matched) + page_size - 1) // page_size
    try:
        page = max(0, min(int(page), total_pages - 1))
    except (TypeError, ValueError):
        page = 0
    page_rows = matched[page * page_size:(page + 1) * page_size]
    buttons = []
    for row in page_rows:
        try:
            price = float(row["sale_price"] or 0)
        except (TypeError, ValueError):
            price = 0
        package_name = str(row["package_name"] or "Paket").strip()
        # Tugma matni qisqa bo'lsin, uzun katalog nomlari markupni haddan tashqari kattalashtirmasin.
        if len(package_name) > 48:
            package_name = package_name[:45] + "..."
        game_id = int(row["game_id"])
        paket_id = int(row["paket_id"])
        if price > 0:
            button_text = f"{package_name} — {price:,.0f} so'm"
            buttons.append([InlineKeyboardButton(
                button_text[:64], callback_data=f"b2tg_pkg_{game_id}_{paket_id}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                f"{package_name[:40]} — Narx yuklanmagan",
                callback_data="b2tg_noprice",
            )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Oldingi", callback_data=f"b2tg_page_{service_type}_{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Keyingi ➡️", callback_data=f"b2tg_page_{service_type}_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Xizmatlarga qaytish", callback_data="telegram_services")])
    await _b2tg_edit_or_replace(
        q,
        f"📦 {label} paketlari (sahifa {page+1}/{total_pages}):",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=None,
    )


async def bay2game_telegram_no_price(update, context):
    q = update.callback_query
    await q.message.reply_text("⚠️ Bu paket uchun narx yuklanmagan. Iltimos, boshqa paketni tanlang yoki keyinroq urinib ko'ring.")


async def bay2game_telegram_game(update, context, game_id):
    q = update.callback_query
    c = conn()
    try:
        game = c.execute("SELECT name FROM games WHERE game_id=? AND active=1", (game_id,)).fetchone()
        products = c.execute(
            "SELECT paket_id,package_name,sale_price FROM products WHERE game_id=? AND active=1 ORDER BY paket_id",
            (game_id,),
        ).fetchall()
    finally:
        c.close()
    if not game or not products:
        await q.message.reply_text("❌ Paket topilmadi. Admin katalogni yangilasin.")
        return
    context.user_data.clear()
    rows = []
    for item in products:
        try:
            price = float(item["sale_price"] or 0)
        except Exception:
            price = 0
        if price <= 0:
            continue
        rows.append([InlineKeyboardButton(
            f"{item['package_name']} — {price:,.0f} so'm",
            callback_data=f"b2tg_pkg_{game_id}_{int(item['paket_id'])}"
        )])
    rows.append([InlineKeyboardButton("🔙 Orqaga", callback_data="telegram_services")])
    if len(rows) == 1:
        await q.message.reply_text("❌ Ushbu xizmat uchun narxli paket topilmadi.")
        return
    await q.message.reply_text(f"📦 {game['name']} — paketni tanlang:", reply_markup=InlineKeyboardMarkup(rows))


async def bay2game_telegram_package(update, context, game_id, paket_id):
    q = update.callback_query
    c = conn()
    try:
        row = c.execute(
            "SELECT g.name AS game_name,p.package_name,p.sale_price FROM games g JOIN products p ON p.game_id=g.game_id WHERE g.game_id=? AND p.paket_id=? AND g.active=1 AND p.active=1",
            (game_id, paket_id),
        ).fetchone()
    finally:
        c.close()
    if not row or float(row["sale_price"] or 0) <= 0:
        await q.message.reply_text("❌ Paket yoki narx topilmadi. Katalogni yangilang.")
        return
    context.user_data["b2tg_game_id"] = game_id
    context.user_data["b2tg_paket_id"] = paket_id
    context.user_data["b2tg_game_name"] = row["game_name"]
    context.user_data["b2tg_product_name"] = row["package_name"]
    context.user_data["b2tg_price"] = float(row["sale_price"])
    context.user_data["state"] = "b2tg_username"
    await _b2tg_edit_or_replace(
        q,
        f"📦 {row['game_name']}\n🎁 {row['package_name']}\n💰 {float(row['sale_price']):,.0f} so'm\n\n"
        "👤 Qabul qiluvchining Telegram username'ini yuboring (@username).",
        reply_markup=None,
        parse_mode=None,
    )
    context.user_data["b2tg_prompt_message_id"] = q.message.message_id
    context.user_data["b2tg_prompt_chat_id"] = q.message.chat_id


async def bay2game_telegram_confirm(update, context):
    q = update.callback_query
    uid = q.from_user.id
    started_at = b2tg_now_text()
    game_id = int(context.user_data.get("b2tg_game_id", 0) or 0)
    paket_id = int(context.user_data.get("b2tg_paket_id", 0) or 0)
    username = str(context.user_data.get("b2tg_username", "")).strip().lstrip("@")
    price = float(context.user_data.get("b2tg_price", 0) or 0)
    game_name = str(context.user_data.get("b2tg_game_name", "Telegram service"))
    product_name = str(context.user_data.get("b2tg_product_name", "Package"))
    if not game_id or not paket_id or not username or price <= 0:
        await _b2tg_edit_or_replace(q, "❌ Buyurtma ma'lumotlari eskirgan. Qaytadan urinib ko'ring.", reply_markup=None, parse_mode=None)
        context.user_data.clear()
        return
    balance_before = get_balance(uid)
    if balance_before < Decimal(str(price)):
        await _b2tg_edit_or_replace(q, f"❌ Balans yetarli emas.\nKerak: {price:,.0f} so'm\nBalans: {balance_before:,.0f} so'm", reply_markup=None, parse_mode=None)
        return

    add_balance(uid, -price, "purchase", f"Telegram {game_name} {product_name}")
    api_status, api_data = None, {}
    order_id = "-"
    try:
        api_status, api_data = await asyncio.to_thread(api_post, "/order", {
            "game_id": game_id, "paket_id": paket_id, "player_id": username
        })
        if not isinstance(api_data, dict):
            api_data = {"response": str(api_data)}
        # ID/ref ni xato tekshiruvidan oldin ham ajratamiz — admin hisobotida yo'qolmasin.
        raw_data = api_data.get("raw") if isinstance(api_data.get("raw"), dict) else {}
        nested_data = raw_data.get("data") if isinstance(raw_data.get("data"), dict) else raw_data
        nested_order = nested_data.get("order") if isinstance(nested_data.get("order"), dict) else nested_data
        order_id = str(
            api_data.get("order_id") or api_data.get("reference") or
            nested_order.get("order_id") or nested_order.get("reference") or "-"
        )
        if not api_data.get("ok"):
            raise RuntimeError(str(api_data.get("error") or api_data.get("message") or api_data)[:500])
        if not order_id:
            # API javobida ID bo'lmasa ham, bu yetkazib berilmaganini anglatmaydi.
            raise RuntimeError("API javobida buyurtma ID yo'q; yetkazib berish holati tekshirilishi kerak")
        service_type = "stars" if "star" in (game_name + " " + product_name).lower() else "premium"
        save_external_order(uid, "bay2game", service_type, order_id, "@" + username, price=price, status=str(api_data.get("status", "processing")))
        final_status = str(api_data.get("status", "processing"))
        await _b2tg_edit_or_replace(
            q,
            f"✅ Buyurtma qabul qilindi!\n\n🆔 Buyurtma: {order_id}\n👤 @{username}\n📦 {game_name} — {product_name}\n💰 {price:,.0f} so'm\n📊 Holat: {final_status}\n🕒 {started_at}",
            reply_markup=None, parse_mode=None,
        )
        await b2tg_admin_report(
            context, result="QABUL QILINDI", uid=uid, username=username,
            game_name=game_name, product_name=product_name, price=price,
            started_at=started_at, order_id=order_id, http_status=api_status,
            api_data=api_data, balance_action=f"-{price:,.0f} so'm (xarid)",
        )
        await notify_order_channel(
            context,
            f"⭐ TELEGRAM BUYURTMA #{order_id}\n\n"
            f"👤 Telegram ID: {uid}\n📨 Qabul qiluvchi: @{username}\n"
            f"📦 {game_name} — {product_name}\n💰 {price:,.0f} so'm\n"
            f"📊 Holat: {final_status}\n🕒 {started_at}"
        )
        context.user_data.clear()
    except Exception as exc:
        log.exception("Telegram Stars/Premium buyurtmasida xato")
        provider_status = _b2tg_provider_status(api_data)
        # Faqat provayder aniq yakuniy rad etgan bo'lsa qaytaramiz.
        # Timeout, HTTP 402 yoki noaniq javobda mahsulot yetib borishi mumkin;
        # bunday vaziyatda avtomatik refund ikki marta foyda berib qo'yishi mumkin.
        definite_failure = provider_status in ("failed", "failure", "rejected", "cancelled", "canceled")
        if definite_failure:
            add_balance(uid, price, "refund", f"Telegram buyurtma qaytarildi: {product_name}")
            balance_action = f"+{price:,.0f} so'm (aniq rad etildi, refund)"
            result = "RAD ETILDI — PUL QAYTARILDI"
            user_text = "❌ Buyurtma rad etildi. Pul balansingizga qaytarildi."
        else:
            balance_action = f"-{price:,.0f} so'm ushlab turildi; admin tekshirishi kerak"
            result = "NOANIQ — ADMIN TEKSHIRUVI KERAK"
            user_text = "⏳ Buyurtma holati tekshirilmoqda. Mahsulot kelgan bo'lishi mumkin, shu sabab pul avtomatik qaytarilmadi. Admin tekshiradi."
        await _b2tg_edit_or_replace(q, user_text + f"\n🕒 {started_at}", reply_markup=None, parse_mode=None)
        await b2tg_admin_report(
            context, result=result + f" | Xato: {str(exc)[:400]}", uid=uid,
            username=username, game_name=game_name, product_name=product_name,
            price=price, started_at=started_at, order_id=order_id,
            http_status=api_status if api_status is not None else "so'rov xatosi",
            api_data=api_data, balance_action=balance_action,
        )
        await notify_bot_owner_technical(context, uid, str(exc))
        context.user_data.clear()


# ============================================================
# USER
# ============================================================

def ensure_user(u):

    if not u:
        return

    # Foydalanuvchi balansi barcha ulangan botlar uchun
    # asosiy botning DBsida saqlanadi. Shu sabab child botdan
    # kirgan user ham asosiy botdagi o'z balansidan foydalanadi.
    c = None
    try:
        # main_conn() dan foydalanish har doim asosiy DBni beradi.
        c = main_conn()
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
    finally:
        if c is not None:
            c.close()


def user_exists(uid):

    c = main_conn()

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

    c = main_conn()

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

    c = main_conn()

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


def mark_main_bot_user(uid):
    """Return True only the first time this Telegram user starts the main bot."""
    c = main_conn()
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO main_bot_users(user_id, first_started_at) VALUES(?,?)",
            (int(uid), datetime.now().isoformat()),
        )
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def register_referral(invited_uid, payload):
    """Register one valid referral and award the configured bonus once."""
    try:
        referrer_uid = int(str(payload).removeprefix("ref_"))
    except (TypeError, ValueError):
        return None

    if referrer_uid <= 0 or referrer_uid == int(invited_uid):
        return None

    c = main_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        referrer = c.execute(
            "SELECT user_id FROM users WHERE user_id=?", (referrer_uid,)
        ).fetchone()
        already = c.execute(
            "SELECT 1 FROM referrals WHERE invited_user_id=?", (int(invited_uid),)
        ).fetchone()
        if not referrer or already:
            c.rollback()
            return None

        setting = c.execute(
            "SELECT value FROM settings WHERE key='referral_bonus_uzs'"
        ).fetchone()
        try:
            bonus = max(0.0, float(setting["value"] if setting else 100))
        except (TypeError, ValueError):
            bonus = 100.0

        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO referrals(invited_user_id,referrer_user_id,bonus,created_at) VALUES(?,?,?,?)",
            (int(invited_uid), referrer_uid, bonus, now),
        )
        if bonus > 0:
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (bonus, referrer_uid))
            c.execute(
                "INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)",
                (referrer_uid, bonus, "referral_bonus", f"Referal: {invited_uid}", now),
            )
        c.commit()
        return {"referrer_id": referrer_uid, "bonus": bonus}
    except Exception:
        c.rollback()
        log.exception("Referalni ro'yxatdan o'tkazishda xato")
        return None
    finally:
        c.close()


def referral_stats(uid):
    c = main_conn()
    try:
        row = c.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(bonus),0) AS earned FROM referrals WHERE referrer_user_id=?",
            (int(uid),),
        ).fetchone()
        return int(row["total"] or 0), float(row["earned"] or 0)
    finally:
        c.close()


# ============================================================
# BAY2GAME API
# ============================================================

def get_bay2game_usd_uzs():
    """USD kursini DB sozlamasidan oladi; admin o'zgartirishi mumkin."""
    try:
        value = Decimal(str(get_setting("bay2game_usd_uzs", BAY2GAME_USD_UZS)))
        if value > 0:
            return value
    except Exception:
        pass
    return BAY2GAME_USD_UZS

def bay2game_internal_id(value):
    """String game/product code ni DB uchun barqaror sonli ID ga aylantiradi."""
    raw = str(value or "").strip()
    if raw.isdigit():
        number = int(raw)
        # Grand Mobile va eski maxsus IDlar bilan to'qnashmasin.
        if number not in (PUBG_GAME_ID, PUBG_GLOBAL_GAME_ID, MOBILE_LEGENDS_GAME_ID, MOBILE_LEGENDS_GLOBAL_GAME_ID, GRAND_MOBILE_GAME_ID):
            return number
    return 100000000 + (int(hashlib.sha256(raw.encode()).hexdigest()[:12], 16) % 800000000)


def bay2game_request(path, params=None):
    params = dict(params or {})
    params["api_key"] = BAY2GAME_API_KEY
    try:
        response = requests.get(
            f"{BAY2GAME_BASE}/{path.lstrip('/')}",
            params=params,
            timeout=12,
        )
        try:
            data = response.json()
        except Exception:
            data = {"error": response.text[:500]}
        if not isinstance(data, dict):
            data = {"data": data}
        # API javoblari barcha endpointlarda bir xil `ok` maydoniga ega bo'lmasligi mumkin.
        data.setdefault("http_status", response.status_code)
        if response.status_code >= 400:
            data.setdefault("ok", False)
            data.setdefault("error", data.get("message") or f"HTTP {response.status_code}")
        elif "ok" not in data:
            status_value = str(data.get("status", "")).lower()
            explicit_failure = data.get("success") is False
            data["ok"] = (not explicit_failure) and status_value not in ("error", "failed", "failure") and not data.get("error")
        log.info(
            "Provider API request path=%s HTTP=%s status=%s ok=%s error=%s",
            path,
            response.status_code,
            str(data.get("status", ""))[:80],
            data.get("ok"),
            str(data.get("error") or data.get("message") or "")[:300],
        )
        return response.status_code, data
    except Exception as exc:
        log.exception("Bay2Game API so'rovi xatosi: %s", path)
        return 0, {"ok": False, "error": str(exc)}


def api_headers():
    # Eski boshqa kod bilan moslik uchun saqlangan; Bay2Game api_key query parametridan foydalanadi.
    return {"Accept": "application/json", "Content-Type": "application/json"}


def api_get(path, params=None):
    return bay2game_request(path, params)


def get_games_api():
    """Bay2Game /categories javobidagi barcha ichma-ich o'yinlarni yig'adi."""
    status, data = bay2game_request("/categories")
    if not data.get("ok"):
        log.error(
            "Bay2Game categories xatosi: HTTP=%s keys=%s error=%s",
            status, list(data.keys()), data.get("error") or data.get("message")
        )
        return []

    # Javob sxemasi o'zgarishi mumkin. Shu sababli faqat categories/games
    # nomli kalitlarni emas, barcha ichma-ich dict/list qiymatlarini ko'ramiz.
    code_keys = (
        "game_code", "gameCode", "category_code", "categoryCode",
        "game_id", "gameId", "category_id", "categoryId",
        "code", "slug", "id",
    )
    name_keys = (
        "name", "game_name", "gameName", "category_name",
        "categoryName", "title", "slug",
    )
    product_keys = {
        "product_code", "productcode", "product_id", "productid",
        "sku", "sell_price", "price_usd", "package_name",
    }

    found = []
    seen = set()
    visited = set()

    def walk(value, path="root", depth=0):
        if depth > 12:
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", depth + 1)
            return
        if not isinstance(value, dict):
            return

        # Bir xil obyekt turli wrapperlarda takrorlansa, qayta ishlamaymiz.
        marker_obj = id(value)
        if marker_obj in visited:
            return
        visited.add(marker_obj)

        lower_keys = {str(k).lower() for k in value.keys()}
        code = next((value.get(k) for k in code_keys if value.get(k) not in (None, "")), None)
        name = next((value.get(k) for k in name_keys if value.get(k) not in (None, "")), None)
        is_product = bool(lower_keys & product_keys)
        if code is not None and name is not None and not is_product:
            code_text = str(code).strip()
            name_text = str(name).strip()
            unique_key = code_text.lower()
            if code_text and name_text and unique_key not in seen:
                seen.add(unique_key)
                found.append(value)
                log.debug("Bay2Game game topildi: path=%s code=%s name=%s", path, code_text, name_text)

        # API yangi wrapper kalit qo'shsa ham ichidagi ro'yxat yo'qolmasin.
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                walk(child, f"{path}.{key}", depth + 1)

    walk(data)
    if found:
        log.info("Bay2Game katalogidan jami %s ta o'yin topildi (HTTP %s)", len(found), status)
        return found

    # Javob mazmunini maxfiy ma'lumotlarsiz log qilamiz; API key yozilmaydi.
    def describe(value, depth=0):
        if depth > 4:
            return type(value).__name__
        if isinstance(value, dict):
            return {str(k): describe(v, depth + 1) for k, v in list(value.items())[:30]}
        if isinstance(value, list):
            return {"type": "list", "length": len(value), "first_item": describe(value[0], depth + 1) if value else None}
        if isinstance(value, str):
            return value[:100]
        return value

    log.error(
        "Bay2Game /categories javobida o'yinlar topilmadi: HTTP=%s shape=%s",
        status, describe(data)
    )
    return []


def bay2game_game_internal_id(game_code, name=""):
    """PUBG/ML eski IDlarini saqlaydi, qolgan API o'yinlariga barqaror ID beradi."""
    label = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    code = str(game_code or "").strip()
    if "pubg" in label:
        # Global va oddiy PUBG yozuvlarini bitta DB ID ostida birlashtirmaymiz.
        if "global" in label:
            return PUBG_GLOBAL_GAME_ID
        return PUBG_GAME_ID
    if "mobilelegends" in label or "mobilelegend" in label or label in ("mlbb",):
        # Russia (mavjud 54 ID) saqlanadi. Global esa API qaytargan
        # haqiqiy game_code asosida alohida ID oladi; taxminiy ID ishlatilmaydi.
        if "global" in label or "global" in code.lower():
            return bay2game_internal_id("mobile_legends_global:" + code)
        return MOBILE_LEGENDS_GAME_ID
    if "grandmobile" in label:
        return GRAND_MOBILE_GAME_ID
    return bay2game_internal_id(code)


def _field_names(game_fields):
    names = []
    if isinstance(game_fields, list):
        for field in game_fields:
            if isinstance(field, str):
                names.append(field.lower())
            elif isinstance(field, dict):
                names.extend(str(field.get(k, "")).lower() for k in ("name", "code", "key", "field"))
    elif isinstance(game_fields, dict):
        names.extend(str(k).lower() for k in game_fields.keys())
        names.extend(str(v).lower() for v in game_fields.values() if isinstance(v, str))
    return re.sub(r"[^a-z0-9]", "", " ".join(names))


def get_packages_api(game_id):
    """API qaytargan PUBG va boshqa o'yinlarning barcha paketlarini oladi."""
    c = conn()
    try:
        row = c.execute(
            "SELECT provider_game_code,name FROM games WHERE game_id=?",
            (int(game_id),),
        ).fetchone()
    finally:
        c.close()

    game_code = str(row["provider_game_code"] or "") if row else ""
    if not game_code:
        game_code = str(game_id)

    status, data = bay2game_request("/products", {"game_code": game_code})
    if not data.get("ok"):
        log.error("Bay2Game products xatosi game_code=%s: %s", game_code, data.get("error", data))
        return None

    # API turli javob formatlaridan foydalanishi mumkin:
    # products/items/results, yoki data ichidagi shu maydonlar.
    def find_product_list(value, depth=0):
        if depth > 5:
            return None
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items and any(
                any(key in item for key in (
                    "product_code", "productCode", "code", "sku", "product_id", "id"
                ))
                for item in dict_items
            ):
                return dict_items
            # Ba'zi APIlar ro'yxatni data ichida yana o'rab yuboradi.
            for item in value:
                found = find_product_list(item, depth + 1)
                if found is not None:
                    return found
            return None
        if isinstance(value, dict):
            for key in ("products", "items", "results", "data", "packages", "records"):
                if key in value:
                    found = find_product_list(value[key], depth + 1)
                    if found is not None:
                        return found
        return None

    products = find_product_list(data)
    if products is None:
        log.warning("Bay2Game products javob formatini aniqlab bo'lmadi: keys=%s", list(data.keys()))
        return None

    packages = []
    seen_codes = set()
    for product in products:
        product_code = (
            product.get("product_code") or product.get("productCode") or
            product.get("product_id") or product.get("code") or
            product.get("sku") or product.get("id")
        )
        if product_code in (None, ""):
            continue
        product_code = str(product_code).strip()
        if not product_code or product_code in seen_codes:
            continue
        seen_codes.add(product_code)

        # Narx API formatiga qarab sell_price, price yoki nested price ichida kelishi mumkin.
        raw_price = product.get("sell_price")
        if raw_price is None:
            raw_price = product.get("price_usd")
        if raw_price is None:
            raw_price = product.get("price")
        if isinstance(raw_price, dict):
            raw_price = raw_price.get("usd", raw_price.get("amount", raw_price.get("value", 0)))
        try:
            usd = Decimal(str(raw_price or 0))
        except Exception:
            log.warning("Bay2Game mahsulot narxi noto'g'ri: code=%s", product_code)
            continue
        if usd < 0:
            continue

        packages.append({
            "paket_id": bay2game_internal_id(product_code),
            "product_code": product_code,
            "name": str(
                product.get("name") or product.get("product_name") or
                product.get("title") or product.get("amount") or product_code
            ),
            "price": {
                "usd": str(usd),
                "amount": str(usd * get_bay2game_usd_uzs()),
            },
            "sell_price": str(usd),
            "status": product.get("status", "active"),
        })

    log.info("Bay2Game products olindi | game_code=%s | packages=%s", game_code, len(packages))
    return {
        "game_name": row["name"] if row else game_code,
        "game_code": game_code,
        "packages": packages,
    }


def playpay_check_id(game_id, player_id, server_id=None, charname=None):
    """Faqat o'yinchi ID/nikini tekshiradi. PlayPay katalogi yoki buyurtma API ishlatilmaydi."""
    player_id = str(player_id or "").strip()
    if not player_id:
        return 400, {"ok": False, "valid": False, "error": "Player ID kiritilmagan."}
    if not PLAYPAY_API_KEY:
        return 503, {"ok": False, "valid": False, "error": "ID tekshirish sozlanmagan."}

    try:
        c = main_conn()
        local_game = c.execute(
            "SELECT name, provider_game_code FROM games WHERE game_id=? LIMIT 1",
            (int(game_id),)
        ).fetchone()
        c.close()
        if not local_game:
            return 404, {"ok": False, "valid": False, "error": "O'yin katalogda topilmadi."}

        game_name = str(local_game["name"] or "").strip().lower()
        game_code = str(local_game["provider_game_code"] or "").strip().lower()
        if not game_code:
            if "pubg" in game_name:
                game_code = "pubgm"
            elif "mobile legends" in game_name or "mlbb" in game_name:
                game_code = "mlbb"
            elif "free fire" in game_name:
                game_code = "freefire"
            else:
                return 400, {"ok": False, "valid": False, "error": "Bu o'yin uchun tekshiruv kodi sozlanmagan."}

        payload = {"game": game_code, "user_id": player_id}
        if server_id:
            payload["server_id"] = str(server_id).strip()
        if charname:
            payload["charname"] = str(charname).strip()

        response = requests.post(
            f"{PLAYPAY_BASE}/games/checkPlayerId",
            headers={"X-API-Key": PLAYPAY_API_KEY, "Accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=25,
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code >= 400:
            return response.status_code, {"ok": False, "valid": False, "error": str(data.get("message") or data.get("error") or "ID tekshirilmadi.")}

        valid_value = data.get("valid")
        valid = (str(valid_value).strip().lower() == "valid") if isinstance(valid_value, str) else bool(valid_value)
        return response.status_code, {
            "ok": True,
            "valid": valid,
            "player_name": str(data.get("name") or "").strip(),
            "game_code": game_code,
            "player_id": player_id,
        }
    except requests.RequestException as exc:
        log.warning("ID tekshirish tarmoq xatosi: %s", type(exc).__name__)
        return 0, {"ok": False, "valid": False, "error": "ID tekshirish vaqtida aloqa xatosi."}
    except Exception:
        log.exception("ID tekshirish ichki xatosi")
        return 0, {"ok": False, "valid": False, "error": "ID tekshirishda ichki xatolik."}


def bay2game_create_order(product_code, player_id, server_id=""):
    reference = "DONUZ-" + uuid.uuid4().hex[:24].upper()
    params = {
        "product_code": str(product_code),
        "game_user_id": str(player_id).strip(),
        "reference": reference,
    }
    if server_id:
        params["game_zone_id"] = str(server_id).strip()
    status, raw = bay2game_request("/create_order", params)
    nested = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    payload = nested.get("order") if isinstance(nested.get("order"), dict) else nested
    order_status = str(payload.get("status") or nested.get("status") or raw.get("status") or "processing").lower()
    failed = (
        status < 200 or status >= 300 or not raw.get("ok")
        or order_status in ("failed", "failure", "error", "rejected", "cancelled")
    )
    if failed:
        error_text = str(
            raw.get("error") or raw.get("message") or
            nested.get("error") or nested.get("message") or
            f"Buyurtma rad etildi (HTTP {status}, status={order_status})."
        )
        log.error(
            "Avtomatik buyurtma API tomonidan rad etildi: HTTP=%s status=%s error=%s response_keys=%s",
            status, order_status, error_text[:500], list(raw.keys())
        )
        return status, {"ok": False, "error": error_text, "raw": raw}
    provider_reference = str(payload.get("reference") or nested.get("reference") or raw.get("reference") or reference)
    normalized_status = "success" if order_status in ("success", "completed", "complete", "done") else "processing"
    return status, {
        "ok": True,
        "order_id": provider_reference,
        "reference": provider_reference,
        "status": normalized_status,
        "price": {"amount": payload.get("amount_usd", payload.get("sell_price", payload.get("amount", 0)))},
        "charged": {"amount": payload.get("amount_usd", payload.get("sell_price", payload.get("amount", 0)))},
        "raw": raw,
    }


def api_post_no_idempotency(path, body):
    if path == "/check_id":
        return playpay_check_id(body.get("game_id"), body.get("player_id"), body.get("server_id"), body.get("charname"))
    return 400, {"ok": False, "error": "Bu so'rov qo'llab-quvvatlanmaydi."}


def api_post(path, body):
    if path != "/order":
        return 400, {"ok": False, "error": "Xizmat manzili topilmadi."}
    try:
        game_id = int(body.get("game_id"))
        paket_id = int(body.get("paket_id"))
        player_id = str(body.get("player_id") or "").strip()
        server_id = str(body.get("server_id") or "").strip()
        c = conn()
        product = c.execute(
            "SELECT provider_product_code,price_usd FROM products WHERE game_id=? AND paket_id=? AND active=1",
            (game_id, paket_id),
        ).fetchone()
        c.close()
        if not product or not product["provider_product_code"]:
            return 400, {"ok": False, "error": "Mahsulot kodi topilmadi. Katalogni yangilang."}
        return bay2game_create_order(product["provider_product_code"], player_id, server_id)
    except Exception as exc:
        log.exception("Bay2Game order adapter xatosi")
        return 0, {"ok": False, "error": str(exc)}


def get_playpay_order(order_id):
    status, data = bay2game_request("/check_order", {"reference": str(order_id)})
    if not data.get("ok"):
        return status, data
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    order = nested.get("order") if isinstance(nested.get("order"), dict) else nested
    order_status = str(order.get("status") or nested.get("status") or data.get("status") or "processing").lower()
    if order_status in ("completed", "complete", "done"):
        order_status = "success"
    return status, {"ok": True, "order": {"status": order_status, "reference": order.get("reference", order_id)}, "raw": data}


def get_playpay_balance():
    """Bay2Game reseller profili va API balansini turli javob shakllaridan o'qiydi."""
    status, data = bay2game_request("/profile")
    if not data.get("ok"):
        return status, data
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else None
    if profile is None and isinstance(nested.get("profile"), dict):
        profile = nested.get("profile")
    if profile is None:
        profile = nested
    balance = profile.get("balance", profile.get("balance_usd", profile.get("api_balance", nested.get("balance", 0))))
    if isinstance(balance, dict):
        amount = balance.get("amount", balance.get("usd", balance.get("value", 0)))
        currency = balance.get("currency", "USD")
    else:
        amount, currency = balance, "USD"
    return status, {
        "ok": True,
        "balance": {"amount": amount, "currency": currency},
        "profile": profile,
        "raw": data,
    }


# ============================================================
# YUPAY — FAQAT ROBLOX KATALOGI VA BUYURTMALARI
# ============================================================

def yupay_request(method, path, body=None):
    """YuPay Merchant API. Bearer kalit yoki hujjatdagi HMAC imzosi bilan ishlaydi."""
    method = str(method or "GET").upper()
    # YUPAY_BASE sifatida domenning o'zi yoki /merchant/v1 bilan tugaydigan
    # manzil berilgan bo'lishi mumkin. Endpointni har doim bitta marta qo'shamiz.
    endpoint = "/merchant/v1" + ("/" + str(path or "").lstrip("/") if path else "")
    base = YUPAY_BASE.rstrip("/")
    if base.endswith("/merchant/v1"):
        base = base[:-len("/merchant/v1")]
    elif base.endswith("/merchant/v1/catalog"):
        base = base

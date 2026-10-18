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
import base64
import html
import re
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

BAY2GAME_API_KEY = os.getenv("BAY2GAME_API_KEY", "").strip()
BAY2GAME_BASE = os.getenv("BAY2GAME_BASE", "https://api.bay2game.xyz/api").rstrip("/")
# Bay2Game narxlari USD da. Kurs Render ENV orqali beriladi; admin ustamasi bot ichida boshqariladi.
try:
    BAY2GAME_USD_UZS = Decimal(os.getenv("BAY2GAME_USD_UZS", "13000"))
except Exception:
    BAY2GAME_USD_UZS = Decimal("13000")

# Eski maxsus IDlar (Grand Mobile narxlari o'zgarmaydi)
PUBG_GAME_ID = 141
PUBG_GLOBAL_GAME_ID = 142  # PUBG Mobile Global, alohida API katalogi bo'lsa
MOBILE_LEGENDS_GAME_ID = 54
GRAND_MOBILE_GAME_ID = 999001

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

# ===================== QO'SHIMCHA PROVIDERLAR =====================
# PayStars: Telegram Stars / Premium
PAYSTARS_API_KEY = os.getenv("PAYSTARS_API_KEY", "").strip()
PAYSTARS_API = os.getenv("PAYSTARS_API", "https://paystars.uz/api/v1").rstrip("/")
PAYSTARS_MARKUP_PERCENT = Decimal("4.5")  # Qattiq 4.5% ustama; Render ENV ta'sir qilmaydi

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
    game_id = int(payload.get("game_id", 0))
    paket_id = int(payload.get("paket_id", 0))
    player_id = str(payload.get("player_id", "")).strip()
    server_id = str(payload.get("server_id", "")).strip()

    if not player_id:
        return {"ok": False, "error": "ID kiritilmagan."}

    c = main_conn()
    p = c.execute(
        """
        SELECT * FROM products
        WHERE game_id=? AND paket_id=? AND active=1
        """,
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

    balance = get_balance(user_id)
    if balance < price:
        return {
            "ok": False,
            "error": "Balans yetarli emas.",
            "balance": float(balance),
            "required": float(price),
        }

    # ID qabul qilinadi; Bay2Game tekshiruvni buyurtma vaqtida bajaradi.
    if game_id != GRAND_MOBILE_GAME_ID:
        check = web_check_id({
            "game_id": game_id,
            "player_id": player_id,
            "server_id": server_id,
        })
        if not check.get("ok"):
            return {
                "ok": False,
                "error": "ID tekshirilmadi.",
                "details": check,
            }

    web_token = uuid.uuid4().hex + uuid.uuid4().hex
    now = datetime.now().isoformat()
    c = main_conn()
    c.execute(
        """
        INSERT INTO web_orders(
            token,user_id,game_id,paket_id,product_name,player_id,
            server_id,price,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            web_token, int(user_id), game_id, paket_id, product_name,
            player_id, server_id, float(price), "processing", now, now
        )
    )
    web_order_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    c.close()

    # Balansni ushlab turamiz.
    add_balance(
        user_id, -price, "order_hold",
        f"Web sayt buyurtma #{web_order_id}: {product_name}"
    )

    # Grand Mobile manual buyurtma
    if game_id == GRAND_MOBILE_GAME_ID:
        c = main_conn()
        cur = c.execute(
            """
            INSERT INTO orders(
                user_id,playpay_order_id,game_id,paket_id,product_name,
                player_id,fields_json,cost_usd,charged_usd,sale_price,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(user_id), f"WEB-{web_order_id}", game_id, paket_id,
                product_name, player_id,
                json.dumps({"player_id": player_id}, ensure_ascii=False),
                0, 0, float(price), "manual_pending", now, now
            )
        )
        local_id = cur.lastrowid
        c.execute(
            "UPDATE web_orders SET status='manual_pending',updated_at=? WHERE id=?",
            (datetime.now().isoformat(), web_order_id)
        )
        c.commit()
        c.close()

        if ADMIN_ID:
            _web_send_telegram_message(
                ADMIN_ID,
                "🆕 WEB GRAND MOBILE BUYURTMA\n\n"
                f"📦 {product_name}\n"
                f"🆔 ID: {player_id}\n"
                f"💰 {price:,.0f} so'm\n"
                f"👤 Telegram ID: {user_id}\n"
                f"📋 Buyurtma: #{local_id}"
            )

        return {
            "ok": True,
            "status": "manual_pending",
            "order_id": local_id,
            "message": "Buyurtma qabul qilindi. Grand Mobile buyurtmasi manual bajariladi.",
        }

    # Bay2Game buyurtma
    body = {
        "game_id": game_id,
        "paket_id": paket_id,
        "player_id": player_id,
    }
    if server_id:
        body["server_id"] = server_id

    status, data = api_post("/order", body)

    if not isinstance(data, dict) or not data.get("ok"):
        add_balance(
            user_id, price, "order_refund",
            f"Web sayt Bay2Game xatosi: {str(data)[:250]}"
        )
        c = main_conn()
        c.execute(
            "UPDATE web_orders SET status='failed',updated_at=? WHERE id=?",
            (datetime.now().isoformat(), web_order_id)
        )
        c.commit()
        c.close()
        return {
            "ok": False,
            "error": "Buyurtmani bajarib bo'lmadi. Pul balansingizga qaytarildi.",
        }

    provider_order_id = str(data.get("order_id", ""))
    c = main_conn()
    c.execute(
        "UPDATE web_orders SET status=?,playpay_order_id=?,updated_at=? WHERE id=?",
        (
            str(data.get("status", "processing")),
            provider_order_id,
            datetime.now().isoformat(),
            web_order_id,
        )
    )
    c.execute(
        """
        INSERT INTO orders(
            user_id,playpay_order_id,game_id,paket_id,product_name,
            player_id,fields_json,cost_usd,charged_usd,sale_price,
            status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(user_id), provider_order_id, game_id, paket_id,
            product_name, player_id,
            json.dumps({"player_id": player_id, "server_id": server_id}, ensure_ascii=False),
            float((data.get("price") or {}).get("amount", 0) or 0),
            float((data.get("charged") or {}).get("amount", 0) or 0),
            float(price), str(data.get("status", "processing")),
            now, now,
        )
    )
    local_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    c.close()

    return {
        "ok": True,
        "status": str(data.get("status", "processing")),
        "order_id": local_id,
        "provider_order_id": provider_order_id,
        "message": "Buyurtma qabul qilindi.",
    }


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
        server = HTTPServer(
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

    c.commit()
    c.close()

    set_default(
        "payment_card",
        PAYMENT_CARD
    )
    set_default("donat_markup_percent", DEFAULT_MARKUP)
    set_default("bay2game_usd_uzs", os.getenv("BAY2GAME_USD_UZS", "13000"))
    set_default("stars_markup_percent", PAYSTARS_MARKUP_PERCENT)
    set_default("premium_markup_percent", PAYSTARS_MARKUP_PERCENT)
    set_default("sim_markup_percent", SIM_MARKUP_PERCENT)
    set_default("paystars_markup_percent", PAYSTARS_MARKUP_PERCENT)
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

        # Bay2Game uses string game_code/product_code; map them to internal numeric IDs.
        for table, cols in {
            "games": {"provider_game_code": "TEXT DEFAULT ''", "game_fields_json": "TEXT DEFAULT '[]'"},
            "products": {"provider_product_code": "TEXT DEFAULT ''"},
        }.items():
            present = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols.items():
                if col not in present:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

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
    row = c.execute("SELECT COUNT(*), COALESCE(SUM(sale_price),0) FROM orders WHERE status NOT IN ('failed','cancelled','rejected')").fetchone()
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
        except Exception:
            log.exception("Child bot start xatosi")


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
# LEGACY PAYSTARS API (Stars/Premium savdosi Bay2Game katalogidan olinadi)
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
    if not PAYSTARS_API_KEY:
        raise RuntimeError("PAYSTARS_API_KEY sozlanmagan")

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
        # API javobidagi foydali xabarni yo'qotmaslik uchun
        # status + xavfsiz detail qaytaramiz. API key hech qachon chiqmaydi.
        detail = data.get("detail", data) if isinstance(data, dict) else data
        if isinstance(detail, dict):
            msg = detail.get("message") or detail.get("error") or detail.get("code") or str(detail)
        else:
            msg = str(detail)
        raise RuntimeError(f"PayStars HTTP {r.status_code}: {msg[:300]}")

    if not isinstance(data, dict):
        raise RuntimeError("PayStars noto'g'ri JSON javob qaytardi")
    return data


def ps_normalize_username(value):
    name = str(value or "").strip().lstrip("@").strip()
    if not name or " " in name or len(name) > 64:
        raise ValueError("Username noto'g'ri")
    return name


def ps_account():
    return ps_get("/account")


def ps_check_user(username, kind):
    name = ps_normalize_username(username)
    if kind not in ("stars", "premium"):
        raise ValueError("Username tekshirish turi noto'g'ri")
    return ps_post(
        "/check-username",
        {"username": name, "kind": kind}
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


def ps_sell(value, kind="stars"):
    key = "stars_markup_percent" if kind == "stars" else "premium_markup_percent"
    try:
        markup = Decimal(str(get_setting(key, PAYSTARS_MARKUP_PERCENT)))
    except Exception:
        markup = PAYSTARS_MARKUP_PERCENT
    return round(float(value) * (1 + float(markup) / 100), 2)


def ps_pricing():
    return ps_account().get("pricing", {})


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
# BAY2GAME TELEGRAM STARS / PREMIUM KATALOGI
# Mahsulot va narx faqat admin yangilaydigan Bay2Game katalogidan olinadi.
# ============================================================
def find_bay2game_telegram_product(kind, amount):
    """Bay2Game katalogida Stars miqdori yoki Premium oyi mos paketni topadi."""
    c = conn()
    try:
        rows = c.execute(
            """SELECT p.game_id, p.paket_id, p.game_name, p.package_name,
                      p.sale_price, p.provider_product_code, g.name AS catalog_game
               FROM products p JOIN games g ON g.game_id=p.game_id
               WHERE p.active=1 AND g.active=1
                 AND COALESCE(p.provider_product_code,'')<>''
               ORDER BY p.game_name, p.package_name"""
        ).fetchall()
    finally:
        c.close()

    wanted = int(amount)
    for row in rows:
        game_name = str(row["catalog_game"] or row["game_name"] or "").lower()
        package = str(row["package_name"] or "").lower()
        combined = game_name + " " + package
        # Avoid unrelated products that happen to contain 'star' or 'premium'.
        if kind == "stars":
            if not ("star" in game_name or "telegram" in game_name) or "premium" in combined:
                continue
            if not ("star" in combined or "telegram" in combined):
                continue
            nums = [int(x) for x in re.findall(r"(?<!\d)\d+(?!\d)", package)]
            if wanted not in nums:
                continue
        else:
            if "premium" not in combined or "telegram" not in combined:
                continue
            nums = [int(x) for x in re.findall(r"(?<!\d)\d+(?!\d)", package)]
            if wanted not in nums:
                continue
        try:
            price = Decimal(str(row["sale_price"] or 0))
        except Exception:
            continue
        if price > 0:
            return {"product_code": str(row["provider_product_code"]),
                    "name": str(row["package_name"] or row["game_name"]),
                    "price": price}
    return None


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
    await q.message.reply_text(
        "⭐ <b>Telegram xizmatlari</b>\n\n"
        "Paketlar va narxlar Bay2Game katalogidan olinadi.\n"
        "Agar kerakli paket ko'rinmasa, admin 🔄 Katalog tugmasi orqali katalogni yangilasin.",
        parse_mode="HTML", reply_markup=paystars_kb()
    )


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
    username = str(context.user_data.get("ps_username") or "").strip().lstrip("@")
    quantity = int(context.user_data.get("ps_quantity", 0) or 0) if kind == "stars" else 0
    months = int(context.user_data.get("ps_months", 0) or 0) if kind == "premium" else 0
    product_code = str(context.user_data.get("ps_product_code") or "").strip()
    try:
        price = Decimal(str(context.user_data.get("ps_price", 0) or 0))
    except Exception:
        price = Decimal("0")
    label = f"⭐ {quantity} Stars" if kind == "stars" else f"💎 Premium {months} oy"

    if not username or not product_code or price <= 0:
        await q.message.reply_text(
            "❌ Buyurtma ma'lumotlari topilmadi. Bay2Game katalogini yangilang va qayta urinib ko'ring."
        )
        context.user_data.clear()
        return
    if get_balance(uid) < price:
        await q.message.reply_text(
            f"❌ Balans yetarli emas.\nKerak: {price:,.0f} so'm\n"
            f"Balans: {get_balance(uid):,.0f} so'm"
        )
        return

    # Balans faqat buyurtma API'ga yuborilishidan oldin vaqtincha yechiladi;
    # API xato qaytarsa to'liq qaytariladi.
    add_balance(uid, -price, "purchase", f"Bay2Game {label}")
    try:
        status, result = await asyncio.to_thread(bay2game_create_order, product_code, username)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Bay2Game buyurtmani qabul qilmadi"))
        oid = str(result.get("order_id") or result.get("reference") or "")
        if not oid:
            raise RuntimeError("Bay2Game order ID qaytarmadi")
        save_external_order(uid, "bay2game", kind, oid, username,
                            quantity, months, float(price), str(result.get("status") or "processing"))
        await q.message.reply_text(
            f"✅ <b>Buyurtma Bay2Game'ga yuborildi!</b>\n\n"
            f"🆔 <code>{oid}</code>\n👤 @{username}\n{label}\n"
            f"💰 {price:,.0f} so'm\n📊 {result.get('status', 'processing')}",
            parse_mode="HTML"
        )
        context.user_data.clear()
    except Exception as e:
        log.exception("Bay2Game Telegram %s order failed: %s", kind, str(e)[:300])
        add_balance(uid, price, "refund", f"Bay2Game {label} refund")
        await notify_bot_owner_technical(context, uid)
        await q.message.reply_text(
            "❌ Bay2Game buyurtmani bajara olmadi.\n\n"
            "💰 Balansingiz qaytarildi. Katalog va mahsulot mavjudligini tekshirib, qayta urinib ko'ring."
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
        if number not in (PUBG_GAME_ID, PUBG_GLOBAL_GAME_ID, MOBILE_LEGENDS_GAME_ID, GRAND_MOBILE_GAME_ID):
            return number
    return 100000000 + (int(hashlib.sha256(raw.encode()).hexdigest()[:12], 16) % 800000000)


def bay2game_request(path, params=None):
    params = dict(params or {})
    params["api_key"] = BAY2GAME_API_KEY
    try:
        response = requests.get(
            f"{BAY2GAME_BASE}/{path.lstrip('/')}",
            params=params,
            timeout=35,
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
    """Bay2Game katalogidagi barcha o'yinlarni turli API javob formatlaridan oladi."""
    status, data = bay2game_request("/categories")
    if not data.get("ok"):
        log.error("Bay2Game categories xatosi: %s", data.get("error", data))
        return []

    # API javobi categories, games yoki data ichida kelishi mumkin.
    candidates = [data.get("categories"), data.get("games"), data.get("items"), data.get("results")]
    nested = data.get("data")
    if isinstance(nested, list):
        candidates.append(nested)
    elif isinstance(nested, dict):
        candidates.extend([
            nested.get("categories"), nested.get("games"), nested.get("items"),
            nested.get("results"), nested.get("data")
        ])

    for categories in candidates:
        if isinstance(categories, list):
            # Faqat o'yin/category obyektlarini qabul qilamiz.
            valid = [x for x in categories if isinstance(x, dict)]
            if valid:
                return valid
    log.error("Bay2Game katalog javobida o'yinlar ro'yxati topilmadi: keys=%s", list(data.keys()))
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
    """Faqat kiritilgan ID mavjudligini tekshiradi; o'yinchi nickini tasdiqlamaydi.

    Hozirgi buyurtma API adapterida ID->nickname tekshiruv endpointi yo'q.
    Shuning uchun IDni nickname deb ko'rsatish yoki valid=True deb yolg'on
    tasdiqlash mumkin emas.
    """
    player_id = str(player_id or "").strip()
    if not player_id:
        return 400, {"ok": False, "valid": False, "error": "Player ID kiritilmagan."}
    return 200, {
        "ok": True,
        "valid": None,
        "player_name": "",
        "message": "Nickname tekshiruvi bu API orqali qo'llab-quvvatlanmaydi.",
        "validation_supported": False,
    }


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
    return 400, {"ok": False, "error": "Bay2Game bu endpointni qo'llamaydi."}


def api_post(path, body):
    if path != "/order":
        return 400, {"ok": False, "error": "Bay2Game endpointi topilmadi."}
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
            return 400, {"ok": False, "error": "Bay2Game product_code topilmadi. Katalogni yangilang."}
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
# NARX
# ============================================================

def calc_sale_price(api_uzs):

    try:

        price = Decimal(
            str(api_uzs)
        )

    except Exception:

        price = Decimal("0")

    try:
        markup = Decimal(str(get_setting("donat_markup_percent", DEFAULT_MARKUP)))
    except Exception:
        markup = DEFAULT_MARKUP

    sale = price * (
        Decimal("1")
        +
        markup / Decimal("100")
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
            updated_at,
            provider_product_code
        )
        VALUES (?,?,?,?,?,?,?,?,?,?)
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
            datetime.now().isoformat(),
            str(package.get("product_code") or "")
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
    """Bay2Game categories va products katalogini yangilaydi."""
    categories = get_games_api()
    if not categories:
        return False, "Bay2Game katalogi olinmadi. API key va API balansini tekshiring."

    game_count = 0
    product_count = 0
    c = conn()
    try:
        c.execute("UPDATE games SET active=0")
        c.execute("UPDATE products SET active=0")
        c.commit()
    finally:
        c.close()

    # PUBG umumiy katalog javobida tushib qolsa, API qidiruvi orqali ham izlaymiz.
    pubg_in_catalog = any(
        isinstance(item, dict) and "pubg" in str(
            item.get("name") or item.get("game_name") or item.get("category_name") or item.get("title") or ""
        ).lower()
        for item in categories
    )
    # PUBG Global API katalogida alohida nom bilan bo'lsa, uni ham izlaymiz.
    # API uni alohida qaytarmasa, oddiy PUBG mahsulotini Global deb taxmin qilmaymiz.
    if not pubg_in_catalog or not any(
        isinstance(item, dict) and "pubg" in str(
            item.get("name") or item.get("game_name") or item.get("category_name") or item.get("title") or ""
        ).lower() and "global" in str(
            item.get("name") or item.get("game_name") or item.get("category_name") or item.get("title") or ""
        ).lower()
        for item in categories
    ):
        for search_term in ("pubg global", "pubg"):
          for path in ("/categories", "/games"):
            st, response = bay2game_request(path, {"search": search_term, "q": search_term})
            if st and response.get("ok"):
                options = []
                for key in ("categories", "games", "items", "results", "data"):
                    val = response.get(key)
                    if isinstance(val, list):
                        options.extend(x for x in val if isinstance(x, dict))
                    elif isinstance(val, dict):
                        for inner in ("categories", "games", "items", "results", "data"):
                            sub = val.get(inner)
                            if isinstance(sub, list):
                                options.extend(x for x in sub if isinstance(x, dict))
                pubg_options = [
                    x for x in options
                    if "pubg" in str(x.get("name") or x.get("game_name") or x.get("category_name") or x.get("title") or "").lower()
                    and "delay" not in str(x.get("name") or x.get("game_name") or x.get("category_name") or x.get("title") or "").lower()
                ]
                if search_term == "pubg global":
                    pubg_options = [x for x in pubg_options if "global" in str(x.get("name") or x.get("game_name") or x.get("category_name") or x.get("title") or "").lower()]
                if pubg_options:
                    categories.extend(pubg_options)
                    break
          if any("global" in str(x.get("name") or x.get("game_name") or x.get("category_name") or x.get("title") or "").lower() for x in categories if isinstance(x, dict)):
            break

    for category in categories:
        if not isinstance(category, dict):
            continue
        game_code = str(
            category.get("game_code") or category.get("gameCode") or category.get("category_code") or
            category.get("categoryCode") or category.get("game_id") or category.get("category_id") or
            category.get("code") or category.get("slug") or category.get("id") or ""
        ).strip()
        name = str(
            category.get("name") or category.get("game_name") or category.get("gameName") or
            category.get("category_name") or category.get("title") or category.get("slug") or game_code
        ).strip()
        # PUBG Mobile Delay oddiy PUBG Mobile emas — uni foydalanuvchi katalogiga qo'shmaymiz.
        if "pubg" in name.lower() and "delay" in name.lower():
            log.info("PUBG Delay katalogdan o'tkazib yuborildi: %s", game_code)
            continue
        # Ayrim provayderlar regionni nomda emas, alohida maydonda qaytaradi.
        region = str(category.get("region") or category.get("server") or category.get("version") or "").strip()
        if "pubg" in name.lower() and region and region.lower() in ("global", "global version", "international") and "global" not in name.lower():
            name = f"{name} Global"
        if not game_code:
            continue
        gid = bay2game_game_internal_id(game_code, name)
        fields = category.get("game_fields") or category.get("gameFields") or category.get("fields") or category.get("required_fields") or category.get("input_fields") or []
        names = _field_names(fields)
        requires_server = "serverid" in names or "zoneid" in names or "gamezoneid" in names
        id_label = "User ID" if "userid" in names else "Player ID"
        try:
            c = conn()
            c.execute(
                "INSERT OR REPLACE INTO games(game_id,name,id_label,requires_server,amount_based,active,updated_at,provider_game_code,game_fields_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (gid, name, id_label, 1 if requires_server else 0, 0, 1, datetime.now().isoformat(), game_code, json.dumps(fields, ensure_ascii=False)),
            )
            c.commit()
            c.close()
            game_count += 1
        except Exception:
            log.exception("Bay2Game game saqlash xatosi: %s", game_code)
            try: c.close()
            except Exception: pass
            continue

        data = get_packages_api(gid)
        if not data:
            continue
        for product in data.get("packages", []):
            try:
                product_code = str(product.get("product_code") or "").strip()
                if not product_code:
                    continue
                paket_id = bay2game_internal_id(product_code)
                usd = Decimal(str(product.get("sell_price", product.get("price", {}).get("usd", 0)) or 0))
                api_uzs = usd * get_bay2game_usd_uzs()
                sale_price = calc_sale_price(api_uzs)
                product_status = str(product.get("status", "active")).lower()
                active = 0 if product_status in ("inactive", "disabled", "unavailable", "out_of_stock") else 1
                c = conn()
                c.execute(
                    "INSERT OR REPLACE INTO products(game_id,paket_id,game_name,package_name,price_usd,api_price_uzs,sale_price,active,updated_at,provider_product_code) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (gid, paket_id, name, str(product.get("name") or product.get("product_name") or product_code), float(usd), float(api_uzs), float(sale_price), active, datetime.now().isoformat(), product_code),
                )
                c.commit()
                c.close()
                product_count += 1
            except Exception:
                log.exception("Bay2Game product saqlash xatosi game=%s", game_code)
                try: c.close()
                except Exception: pass

    if game_count == 0:
        return False, "Bay2Game katalogida saqlash mumkin bo'lgan o'yin topilmadi."
    return True, f"✅ Bay2Game: {game_count} ta o'yin, {product_count} ta paket yangilandi. Kurs: 1 USD = {get_bay2game_usd_uzs():,.0f} so'm."


def add_one_calendar_month(value):
    """Sana/vaqtni keyingi kalendar oyga o'tkazadi; kun bo'lmasa oy oxiriga moslaydi."""
    import calendar
    month_index = value.month  # 1..12
    year = value.year + (1 if month_index == 12 else 0)
    month = 1 if month_index == 12 else month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def process_due_omonats(notify=True):
    """Muddat yetgan omonatni bir marta yopib, asosiy summa + 1% foydani qaytaradi."""
    now = datetime.now()
    due_ids = []
    c = main_conn()
    try:
        due = c.execute(
            "SELECT id FROM omonats WHERE status='active' AND matures_at<=? ORDER BY id",
            (now.isoformat(),)
        ).fetchall()
        due_ids = [int(row["id"]) for row in due]
        for oid in due_ids:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM omonats WHERE id=? AND status='active' AND matures_at<=?",
                (oid, now.isoformat())
            ).fetchone()
            if not row:
                c.commit()
                continue
            total = round(float(row["principal"]) + float(row["profit"]), 2)
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (total, row["user_id"]))
            c.execute(
                "INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)",
                (row["user_id"], total, "omonat_maturity", f"Omonat #{oid}: asosiy summa va 1% foyda", now.isoformat())
            )
            c.execute(
                "UPDATE omonats SET status='matured', paid_at=?, closed_at=? WHERE id=? AND status='active'",
                (now.isoformat(), now.isoformat(), oid)
            )
            c.commit()
            if notify and BOT_TOKEN:
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": int(row["user_id"]),
                            "text": (
                                f"🏦 Omonat #{oid} muddati tugadi!\n\n"
                                f"💰 Omonat: {float(row['principal']):,.0f} so'm\n"
                                f"📈 Foyda (1%): {float(row['profit']):,.0f} so'm\n"
                                f"✅ Balansga qaytarildi: {total:,.0f} so'm"
                            )
                        }, timeout=10
                    )
                except Exception:
                    log.exception("Omonat muddati tugagani haqida xabar yuborilmadi: %s", oid)
    except Exception:
        c.rollback()
        log.exception("Omonatlarni muddatida qaytarish xatosi")
    finally:
        c.close()


def omonat_worker():
    """Render qayta ishga tushganda ham muddati o'tgan omonatlarni tekshiradi."""
    while True:
        try:
            process_due_omonats(notify=True)
        except Exception:
            log.exception("Omonat worker xatosi")
        import time
        time.sleep(30)


async def omonat_menu(update, context):
    q = update.callback_query
    uid = q.from_user.id
    process_due_omonats(notify=True)
    c = main_conn()
    active = c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(principal),0) AS total FROM omonats WHERE user_id=? AND status='active'",
        (uid,)
    ).fetchone()
    c.close()
    await q.message.reply_text(
        "🏦 OMONAT\n\n"
        "📈 Oylik foyda: 1%\n"
        "🗓 Muddat: 1 kalendar oy\n"
        "⏰ Foyda omonat ochilgan vaqtga mos ravishda bir oy o'tgach hisoblanadi.\n"
        "✅ Muddat tugaganda asosiy summa va foyda balansga bir marta qaytariladi.\n"
        "⚠️ Muddatidan oldin yopilsa, faqat asosiy summa qaytadi; foyda hisoblanmaydi.\n\n"
        f"💰 Balans: {get_balance(uid):,.0f} so'm\n"
        f"📂 Faol omonatlar: {int(active['n'])}\n"
        f"🔒 Omonatdagi summa: {float(active['total']):,.0f} so'm",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Omonat ochish", callback_data="omonat_open")],
            [InlineKeyboardButton("📜 Omonat tarixi", callback_data="omonat_history")],
            [InlineKeyboardButton("⬅️ Asosiy menyu", callback_data="back_home")],
        ])
    )


async def omonat_open_start(update, context):
    q = update.callback_query
    context.user_data["state"] = "omonat_amount"
    await q.message.reply_text(
        "➕ Omonat ochish\n\n"
        f"💰 Balansingiz: {get_balance(q.from_user.id):,.0f} so'm\n"
        "Omonatga qo'ymoqchi bo'lgan summani yuboring (so'mda).\n"
        "Bekor qilish: /cancel"
    )


async def omonat_history(update, context):
    q = update.callback_query
    uid = q.from_user.id
    process_due_omonats(notify=True)
    c = main_conn()
    rows = c.execute(
        "SELECT * FROM omonats WHERE user_id=? ORDER BY id DESC LIMIT 15",
        (uid,)
    ).fetchall()
    c.close()
    if not rows:
        return await q.message.reply_text(
            "📜 Omonat tarixi hozircha bo'sh.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Omonat", callback_data="omonat")]])
        )
    lines = ["📜 OMONAT TARIXI\n"]
    buttons = []
    for row in rows:
        started = datetime.fromisoformat(row["started_at"]).strftime("%d.%m.%Y %H:%M")
        matures = datetime.fromisoformat(row["matures_at"]).strftime("%d.%m.%Y %H:%M")
        status = {"active": "🟡 Faol", "matured": "✅ Muddat tugadi", "closed": "⚪ Erta yopildi"}.get(row["status"], row["status"])
        lines.append(
            f"\n#{row['id']} — {status}\n"
            f"💰 Summa: {float(row['principal']):,.0f} so'm\n"
            f"📈 Kutilgan foyda: {float(row['profit']):,.0f} so'm\n"
            f"🗓 Ochilgan: {started}\n"
            f"⏰ Muddat: {matures}"
        )
        if row["status"] == "active":
            buttons.append([InlineKeyboardButton(f"🔒 #{row['id']} omonatni yopish", callback_data=f"omonat_close_{row['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Omonat", callback_data="omonat")])
    await q.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def omonat_close_prompt(update, context, omonat_id):
    q = update.callback_query
    c = main_conn()
    row = c.execute("SELECT * FROM omonats WHERE id=? AND user_id=? AND status='active'", (omonat_id, q.from_user.id)).fetchone()
    c.close()
    if not row:
        return await q.message.reply_text("❌ Faol omonat topilmadi yoki u allaqachon yopilgan.")
    await q.message.reply_text(
        f"⚠️ #{omonat_id} omonatni muddatidan oldin yopmoqchimisiz?\n\n"
        f"💰 Qaytariladigan asosiy summa: {float(row['principal']):,.0f} so'm\n"
        "📉 Muddatidan oldin yopilgani uchun 1% foyda berilmaydi.\n\n"
        "Yopishni tasdiqlaysizmi?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ha, yopish", callback_data=f"omonat_close_yes_{omonat_id}")],
            [InlineKeyboardButton("❌ Yo'q", callback_data="omonat_history")],
        ])
    )


async def omonat_close_confirm(update, context, omonat_id):
    q = update.callback_query
    now = datetime.now().isoformat()
    c = main_conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM omonats WHERE id=? AND user_id=? AND status='active'", (omonat_id, q.from_user.id)).fetchone()
        if not row:
            c.rollback()
            return await q.message.reply_text("❌ Omonat allaqachon yopilgan yoki topilmadi.")
        # Agar worker bu vaqt ichida muddati yetgan omonatni qaytargan bo'lsa, ikkinchi to'lov bo'lmaydi.
        if row["matures_at"] <= now:
            c.rollback()
            process_due_omonats(notify=True)
            return await q.message.reply_text("⏰ Omonat muddati tugagan. Asosiy summa va foyda balansga qaytarildi.")
        amount = float(row["principal"])
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, q.from_user.id))
        c.execute("INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)", (q.from_user.id, amount, "omonat_early_close", f"Omonat #{omonat_id}: muddatidan oldin yopildi", now))
        c.execute("UPDATE omonats SET status='closed', closed_at=? WHERE id=? AND status='active'", (now, omonat_id))
        c.commit()
    except Exception:
        c.rollback()
        log.exception("Omonatni yopishda xatolik")
        return await q.message.reply_text("❌ Omonatni yopib bo'lmadi. Keyinroq urinib ko'ring.")
    finally:
        c.close()
    await q.message.reply_text(
        f"✅ Omonat #{omonat_id} yopildi.\n\n💰 Balansga qaytarildi: {amount:,.0f} so'm\n📈 Foyda hisoblanmadi.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📜 Omonat tarixi", callback_data="omonat_history")], [InlineKeyboardButton("⬅️ Omonat", callback_data="omonat")]])
    )


# ============================================================
# FOYDALANUVCHILAR O'RTASIDA PUL O'TKAZISH
# ============================================================

async def transfer_start(update, context):
    q = update.callback_query
    context.user_data["state"] = "transfer_recipient"
    context.user_data.pop("transfer_to", None)
    context.user_data.pop("transfer_amount", None)
    await q.message.reply_text(
        "💸 <b>Balansdan pul o'tkazish</b>\n\n"
        "Pul yubormoqchi bo'lgan foydalanuvchining Telegram ID raqamini yuboring.\n\n"
        "⚠️ Qabul qiluvchi ushbu botni avval ishga tushirgan bo'lishi kerak.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")]])
    )


async def transfer_confirm(update, context):
    q = update.callback_query
    sender_id = q.from_user.id
    recipient_id = context.user_data.get("transfer_to")
    amount_value = context.user_data.get("transfer_amount")

    try:
        recipient_id = int(recipient_id)
        amount = Decimal(str(amount_value))
    except Exception:
        context.user_data.pop("state", None)
        await q.message.reply_text("❌ O'tkazma ma'lumotlari topilmadi. Qaytadan boshlang.", reply_markup=main_menu())
        return

    if recipient_id == sender_id or amount <= 0:
        await q.message.reply_text("❌ O'zingizga pul yubora olmaysiz yoki summa noto'g'ri.", reply_markup=main_menu())
        context.user_data.clear()
        return

    c = main_conn()
    try:
        # Bir vaqtning o'zida kelgan so'rovlar balansni manfiyga tushirmasligi uchun.
        c.execute("BEGIN IMMEDIATE")
        sender = c.execute("SELECT balance FROM users WHERE user_id=?", (sender_id,)).fetchone()
        recipient = c.execute("SELECT user_id, username, first_name FROM users WHERE user_id=?", (recipient_id,)).fetchone()

        if sender is None:
            c.rollback()
            await q.message.reply_text("❌ Sizning hisobingiz topilmadi. /start bosing.", reply_markup=main_menu())
            context.user_data.clear()
            return
        if recipient is None:
            c.rollback()
            await q.message.reply_text("❌ Qabul qiluvchi topilmadi. U avval botni /start bilan ishga tushirsin.")
            return
        if Decimal(str(sender["balance"] or 0)) < amount:
            c.rollback()
            await q.message.reply_text(
                f"❌ Balansingiz yetarli emas.\n\n💰 Balans: {Decimal(str(sender['balance'] or 0)):,.0f} so'm\n💸 Kerak: {amount:,.0f} so'm",
                reply_markup=main_menu()
            )
            context.user_data.clear()
            return

        cur = c.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (float(amount), sender_id, float(amount))
        )
        if cur.rowcount != 1:
            c.rollback()
            await q.message.reply_text("❌ Balans o'zgargan bo'lishi mumkin. Qayta urinib ko'ring.", reply_markup=main_menu())
            context.user_data.clear()
            return

        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (float(amount), recipient_id))
        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)",
            (sender_id, -float(amount), "transfer_out", f"Pul o'tkazildi: {recipient_id}", now)
        )
        c.execute(
            "INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)",
            (recipient_id, float(amount), "transfer_in", f"Pul qabul qilindi: {sender_id}", now)
        )
        c.commit()
        new_sender_balance = c.execute("SELECT balance FROM users WHERE user_id=?", (sender_id,)).fetchone()["balance"]
        new_recipient_balance = c.execute("SELECT balance FROM users WHERE user_id=?", (recipient_id,)).fetchone()["balance"]
    except Exception:
        c.rollback()
        log.exception("Foydalanuvchilar o'rtasida pul o'tkazish xatosi")
        await q.message.reply_text("❌ O'tkazma bajarilmadi. Mablag'lar o'zgartirilmadi, qayta urinib ko'ring.", reply_markup=main_menu())
        context.user_data.clear()
        return
    finally:
        c.close()

    context.user_data.clear()
    recipient_name = (recipient["first_name"] or "")
    if recipient["username"]:
        recipient_name += f" (@{recipient['username']})"
    await q.message.reply_text(
        "✅ <b>Pul muvaffaqiyatli o'tkazildi!</b>\n\n"
        f"👤 Qabul qiluvchi: {recipient_name or 'Foydalanuvchi'}\n"
        f"🆔 Telegram ID: <code>{recipient_id}</code>\n"
        f"💸 O'tkazildi: <b>{amount:,.0f} so'm</b>\n"
        f"💰 Qolgan balans: <b>{Decimal(str(new_sender_balance)):,.0f} so'm</b>",
        parse_mode="HTML",
        reply_markup=main_menu()
    )
    try:
        await context.bot.send_message(
            chat_id=recipient_id,
            text=("💰 <b>Balansingizga pul tushdi!</b>\n\n"
                  f"👤 Yuboruvchi ID: <code>{sender_id}</code>\n"
                  f"➕ Qabul qilindi: <b>{amount:,.0f} so'm</b>\n"
                  f"💰 Yangi balans: <b>{Decimal(str(new_recipient_balance)):,.0f} so'm</b>"),
            parse_mode="HTML"
        )
    except Exception:
        # Pul o'tkazmasi bajarilgan; bildirishnoma yuborilmasligi hisob-kitobni bekor qilmaydi.
        log.info("Transfer qabul qiluvchiga Telegram bildirishnomasi yuborilmadi: %s", recipient_id)


# ============================================================
# MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 O'yinlar", callback_data="games")],
        [InlineKeyboardButton("🚗 Grand Mobile", callback_data="grand_mobile")],
        [InlineKeyboardButton("⭐ Stars / 💎 Premium", callback_data="paystars")],
        # Virtual raqam orqali SMS tasdiqlashni chetlab o'tishga yordam beradigan
        # xarid oqimi menyuda ko'rsatilmaydi.
        [InlineKeyboardButton("⚙️ Botlar sozlamalari", callback_data="bot_list")],
        [InlineKeyboardButton("💳 Balans to'ldirish", callback_data="deposit")],
        [InlineKeyboardButton("💸 Pul o'tkazish", callback_data="transfer_start")],
        [InlineKeyboardButton("🏦 Omonat", callback_data="omonat")],
        [InlineKeyboardButton("💸 Balans yechish", callback_data="withdraw")],
        [InlineKeyboardButton("🔗 Referal", callback_data="referral")],
        [InlineKeyboardButton("📦 Buyurtmalarim", callback_data="orders")],
        [
            InlineKeyboardButton("🎁 Promo kod", callback_data="promo"),
            InlineKeyboardButton("👤 Profil", callback_data="profile")
        ]
    ])


def admin_kb(context=None):
    # Maxsus platforma tugmalari faqat asosiy DONUZ/Bay2Game admin botida ko'rinadi.
    show_platform_controls = context is not None and not is_child_bot(context)
    rows = [
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
            InlineKeyboardButton("🎮 Donat ustama", callback_data="adm_markup_donat"),
            InlineKeyboardButton("📱 SIM ustama", callback_data="adm_markup_sim")
        ],
        [InlineKeyboardButton("🔗 Referal bonusini sozlash", callback_data="adm_referral_bonus")],
        [
            InlineKeyboardButton("⭐ Stars ustama", callback_data="adm_markup_stars"),
            InlineKeyboardButton("💎 Premium ustama", callback_data="adm_markup_premium")
        ],
        [
            InlineKeyboardButton("💵 Donat narxlari", callback_data="adm_prices")
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
        [InlineKeyboardButton("📢 Post", callback_data="adm_post")],
        [
            InlineKeyboardButton("🤖 Bot boshqarish", callback_data="adm_bots"),
        ],
    ]
    if show_platform_controls:
        rows.insert(6, [InlineKeyboardButton("💱 Bay2Game USD kursi", callback_data="adm_bay2game_rate")])
        rows.append([InlineKeyboardButton("🔄 Katalog", callback_data="a_sync")])
        rows.append([InlineKeyboardButton("🔐 Bay2Game balansi", callback_data="adm_playpay_balance")])
        rows.append([InlineKeyboardButton("⭐ PayStars balansi", callback_data="adm_paystars_balance")])
    return InlineKeyboardMarkup(rows)


# ============================================================
# START
# ============================================================

async def start(update, context):

    set_request_db(context)
    if not await child_platform_access(update, context):
        return
    is_main_bot = int(getattr(context.bot, "id", 0) or 0) == int(MAIN_BOT_ID or 0)
    is_new_main_user = mark_main_bot_user(update.effective_user.id) if is_main_bot else False
    ensure_user(update.effective_user)

    # Referal faqat asosiy DONUZ botini birinchi marta ochgan yangi foydalanuvchi uchun hisoblanadi.
    referral_result = None
    if (
        is_new_main_user
        and update.message
        and context.args
        and context.args[0].startswith("ref_")
    ):
        referral_result = register_referral(update.effective_user.id, context.args[0])
        if referral_result:
            try:
                await context.bot.send_message(
                    chat_id=referral_result["referrer_id"],
                    text=(
                        "🎉 Referalingiz botga qo'shildi!\n"
                        f"💰 Bonus: {referral_result['bonus']:,.0f} so'm balansingizga qo'shildi."
                    ),
                )
            except Exception:
                pass

    if not update.message:
        return

    await update.message.reply_text(
        "Assalomu Aleykum 👋\n\n"
        "🎮 Donat botiga xush kelibsiz!",
        reply_markup=main_menu()
    )


# ============================================================
# REFERAL VA BALANS YECHISH
# ============================================================

async def referral_menu(update, context):
    q = update.callback_query
    uid = q.from_user.id
    bonus_raw = get_setting("referral_bonus_uzs", "100")
    try:
        bonus = float(bonus_raw)
    except (TypeError, ValueError):
        bonus = 100.0
    count, earned = referral_stats(uid)
    username = MAIN_BOT_USERNAME
    if not username:
        try:
            me = await context.bot.get_me()
            username = me.username or ""
        except Exception:
            username = ""
    link = f"https://t.me/{username}?start=ref_{uid}" if username else "Bot username topilmadi."
    await q.message.reply_text(
        "🔗 <b>Sizning referal havolangiz</b>\n\n"
        f"{link}\n\n"
        f"👥 Taklif qilganlaringiz: <b>{count}</b> ta\n"
        f"💰 Jami referal bonusi: <b>{earned:,.0f} so'm</b>\n"
        f"🎁 Har bir yangi foydalanuvchi uchun: <b>{bonus:,.0f} so'm</b>\n\n"
        "Bonus faqat yangi foydalanuvchi havola orqali botni birinchi marta ochganda beriladi.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menyu", callback_data="back_home")]]),
        disable_web_page_preview=True,
    )


async def withdrawal_start(update, context):
    q = update.callback_query
    uid = q.from_user.id
    balance = get_balance(uid)
    if balance < 50000:
        return await q.message.reply_text(
            "❌ Balans yechish uchun kamida 50 000 so'm kerak.\n\n"
            f"💰 Hozirgi balansingiz: {balance:,.0f} so'm",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menyu", callback_data="back_home")]]),
        )
    context.user_data["state"] = "withdraw_amount"
    context.user_data.pop("withdraw_amount", None)
    await q.message.reply_text(
        "💸 <b>Balans yechish</b>\n\n"
        f"💰 Balansingiz: {balance:,.0f} so'm\n"
        "📉 Minimal yechish: 50 000 so'm\n\n"
        "Yechmoqchi bo'lgan summangizni yuboring:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")]]),
    )


async def handle_withdrawal_text(update, context, state, text):
    uid = update.effective_user.id
    if state == "withdraw_amount":
        try:
            amount_text = text.lower().replace("so'm", "").replace(" ", "").replace(",", "")
            if amount_text.count(".") == 1 and len(amount_text.split(".")[-1]) == 3:
                amount_text = amount_text.replace(".", "")
            amount = Decimal(amount_text)
        except (InvalidOperation, ValueError):
            await update.message.reply_text("❌ Summani raqam bilan yuboring. Masalan: 50000")
            return True
        if amount != amount.to_integral_value() or amount < 50000:
            await update.message.reply_text("❌ Minimal yechish summasi 50 000 so'm. Butun summani yuboring.")
            return True
        if amount > get_balance(uid):
            await update.message.reply_text(
                f"❌ Balansingiz yetarli emas. Hozirgi balans: {get_balance(uid):,.0f} so'm."
            )
            return True
        context.user_data["withdraw_amount"] = int(amount)
        context.user_data["state"] = "withdraw_details"
        await update.message.reply_text(
            f"💸 Yechiladigan summa: {amount:,.0f} so'm\n\n"
            "Pul tushishi uchun karta raqami yoki to'lov rekvizitlarini yuboring.\n"
            "Masalan: karta raqami va karta egasining ismi."
        )
        return True

    if state == "withdraw_details":
        details = text.strip()
        if len(details) < 5 or len(details) > 300:
            await update.message.reply_text("❌ Rekvizitlarni to'g'ri yuboring (5–300 belgi).")
            return True
        amount = int(context.user_data.get("withdraw_amount", 0) or 0)
        if amount < 50000:
            context.user_data.clear()
            await update.message.reply_text("❌ So'rov bekor qilindi. Qaytadan boshlang.", reply_markup=main_menu())
            return True

        c = main_conn()
        request_id = None
        try:
            c.execute("BEGIN IMMEDIATE")
            user_row = c.execute("SELECT balance, username, first_name FROM users WHERE user_id=?", (uid,)).fetchone()
            if not user_row or float(user_row["balance"] or 0) < amount:
                c.rollback()
                context.user_data.clear()
                await update.message.reply_text("❌ Balansingiz yetarli emas. So'rov yaratilmadi.", reply_markup=main_menu())
                return True
            now = datetime.now().isoformat()
            c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid))
            c.execute(
                "INSERT INTO withdrawals(user_id,amount,payment_details,status,created_at) VALUES(?,?,?,'pending',?)",
                (uid, amount, details, now),
            )
            request_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            c.execute(
                "INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)",
                (uid, -amount, "withdrawal_pending", f"Yechish so'rovi #{request_id}", now),
            )
            c.commit()
        except Exception:
            c.rollback()
            log.exception("Balans yechish so'rovi yaratilmadi")
            await update.message.reply_text("❌ So'rovni yaratishda xatolik. Keyinroq urinib ko'ring.")
            return True
        finally:
            c.close()

        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Balans yechish so'rovi yuborildi!\n\n"
            f"🆔 So'rov: #{request_id}\n"
            f"💰 Summa: {amount:,.0f} so'm\n"
            "⏳ Admin tekshiruvidan keyin to'lov amalga oshiriladi.",
            reply_markup=main_menu(),
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💸 <b>Yangi balans yechish so'rovi #{request_id}</b>\n\n"
                    f"👤 {html.escape(user_row['first_name'] or 'Foydalanuvchi')} (@{html.escape(user_row['username'] or 'yo‘q')})\n"
                    f"🆔 User ID: <code>{uid}</code>\n"
                    f"💰 Summa: <b>{amount:,.0f} so'm</b>\n"
                    f"💳 Rekvizitlar: <code>{html.escape(details)}</code>"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ To'landi", callback_data=f"adm_wd_ok_{request_id}"),
                    InlineKeyboardButton("❌ Rad etish", callback_data=f"adm_wd_no_{request_id}"),
                ]]),
            )
        except Exception:
            log.exception("Admin balans yechish so'rovi haqida xabardor qilinmadi")
        return True
    return False


# ============================================================
# GRAND MOBILE — MANUAL BUYURTMA
# Narxlar foydalanuvchi bergan ro'yxat bo'yicha o'zgarmaydi.
# ============================================================

async def grand_mobile(update, context):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass

    kb = []
    for gc, price in GRAND_MOBILE_PRICES.items():
        kb.append([InlineKeyboardButton(
            f"📱 {gc} GC — {price:,.0f} so'm 💸",
            callback_data=f"grand:{gc}"
        )])

    await q.message.reply_text(
        "🚗 Grand Mobile ID orqali 💸\n\n"
        "📱 Paketni tanlang:\n\n"
        "Oddiy narx GRAND MOBILE da nechi X bo'lsa,\n"
        "shuncha baravar ko'p GC tushadi.\n"
        "Misol: 5X → 1 GC o'rniga 5 GC tushadi.",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def grand_package(update, context):
    q = update.callback_query
    try:
        await q.answer()
        gc = int(q.data.split(":", 1)[1])
    except Exception:
        return await q.message.reply_text("❌ Grand Mobile paketi xato.")

    if gc not in GRAND_MOBILE_PRICES:
        return await q.message.reply_text("❌ Paket topilmadi.")

    context.user_data.clear()
    context.user_data.update({
        "grand_gc": gc,
        "grand_price": Decimal(str(GRAND_MOBILE_PRICES[gc])),
        "state": "grand_player_id",
    })

    await q.message.reply_text(
        f"🚗 Grand Mobile\n\n"
        f"📦 {gc} GC\n"
        f"💰 {GRAND_MOBILE_PRICES[gc]:,.0f} so'm\n\n"
        "🆔 Grand Mobile ID ni yuboring:\n\n"
        "⚠️ Bu buyurtma manual tekshiriladi.\n"
        "Bekor qilish: /cancel"
    )


async def grand_confirm(update, context):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass

    uid = q.from_user.id
    gc = int(context.user_data.get("grand_gc", 0))
    price = Decimal(str(context.user_data.get("grand_price", 0)))
    player_id = str(context.user_data.get("grand_player_id", "")).strip()

    if gc not in GRAND_MOBILE_PRICES or not player_id:
        await q.message.reply_text("❌ Buyurtma ma'lumotlari to'liq emas.")
        context.user_data.clear()
        return

    balance = Decimal(str(get_balance(uid)))
    if balance < price:
        await q.message.reply_text(
            f"❌ Balans yetarli emas.\n\n"
            f"💰 Kerak: {price:,.0f} so'm\n"
            f"💳 Balans: {balance:,.0f} so'm",
            reply_markup=main_menu()
        )
        return

    add_balance(uid, -price, "order_hold", f"Grand Mobile manual: {gc} GC")

    c = conn()
    cur = c.execute(
        """INSERT INTO orders
        (user_id,playpay_order_id,game_id,paket_id,product_name,player_id,fields_json,
         cost_usd,charged_usd,sale_price,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, "MANUAL", GRAND_MOBILE_GAME_ID, gc, f"Grand Mobile {gc} GC",
         player_id, json.dumps({"grand_gc": gc, "manual": True}, ensure_ascii=False),
         0, 0, float(price), "manual_pending", datetime.now().isoformat(), datetime.now().isoformat())
    )
    order_id = cur.lastrowid
    c.commit()
    c.close()

    context.user_data.clear()

    await q.message.reply_text(
        f"✅ Grand Mobile buyurtma qabul qilindi!\n\n"
        f"📦 {gc} GC\n"
        f"🆔 ID: {player_id}\n"
        f"💰 {price:,.0f} so'm\n"
        f"🔢 Buyurtma: #{order_id}\n"
        "📋 Status: Manual tekshiruvda",
        reply_markup=main_menu()
    )

    try:
        await context.bot.send_message(
            bot_admin_id(context),
            f"🚗 GRAND MOBILE MANUAL BUYURTMA #{order_id}\n\n"
            f"👤 User ID: {uid}\n"
            f"📦 {gc} GC\n"
            f"🆔 Grand Mobile ID: {player_id}\n"
            f"💰 Sotuv: {price:,.0f} so'm\n"
            "📋 Status: Manual tekshiruvda"
        )
    except Exception:
        log.exception("Grand Mobile admin xabari xatosi")


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
                WHEN game_id=? THEN 2
                ELSE 3
            END,
            name
        """,
        (
            PUBG_GAME_ID,
            PUBG_GLOBAL_GAME_ID,
            MOBILE_LEGENDS_GAME_ID
        )
    ).fetchall()

    c.close()

    # Agar katalog hali yuklanmagan bo'lsa, foydalanuvchi
    # "🎮 O'yinlar" tugmasini bosganda avtomatik yangilaymiz.
    if not rows:
        try:
            ok, msg = await asyncio.to_thread(sync_catalog)
        except Exception as e:
            ok, msg = False, str(e)

        if ok:
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
                        WHEN game_id=? THEN 2
                        ELSE 3
                    END,
                    name
                """,
                (PUBG_GAME_ID, PUBG_GLOBAL_GAME_ID, MOBILE_LEGENDS_GAME_ID)
            ).fetchall()
            c.close()

    if not rows:
        await q.message.reply_text(
            "❌ Xatolik"
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
        game_id = int(q.data.split(":", 1)[1])
    except Exception:
        await q.message.reply_text("❌ O'yin ID xato.")
        return

    try:
        await q.answer()
    except Exception:
        pass

    c = conn()
    g = c.execute(
        "SELECT * FROM games WHERE game_id=? AND active=1",
        (game_id,)
    ).fetchone()
    rows = c.execute(
        """
        SELECT * FROM products
        WHERE game_id=? AND active=1
        ORDER BY paket_id
        """,
        (game_id,)
    ).fetchall()
    c.close()

    # Paketlar lokal DB'da bo'lmasa, aynan tanlangan o'yin paketlarini
    # Bay2Game katalogidan yangilaymiz.
    if g and not rows:
        try:
            data = await asyncio.to_thread(get_packages_api, game_id)
            if data:
                for package in data.get("packages", []):
                    if package.get("paket_id"):
                        save_package(game_id, g["name"], package)
        except Exception:
            log.exception("Tanlangan o'yin paketlarini yangilash xatosi: %s", game_id)

        c = conn()
        rows = c.execute(
            """
            SELECT * FROM products
            WHERE game_id=? AND active=1
            ORDER BY paket_id
            """,
            (game_id,)
        ).fetchall()
        c.close()

    if not g:
        await q.message.reply_text(
            "❌ O'yin topilmadi.\n\n"
            "Iltimos, keyinroq tugmasini bosib yangilang."
        )
        return

    if not rows:
        await q.message.reply_text(
            f"❌ {g['name']} uchun paketlar hozircha mavjud emas.\n\n"
            "🛠 Texnik nosozlik yuz berdi. Keyinroq qayta urinib ko'ring."
        )
        return

    game_name = g["name"]
    id_label = g["id_label"] or "Player ID"
    requires_server = bool(g["requires_server"])

    if game_id in (PUBG_GAME_ID, PUBG_GLOBAL_GAME_ID):
        id_label = "Player ID"
        requires_server = False
    elif game_id == MOBILE_LEGENDS_GAME_ID:
        id_label = "User ID"
        requires_server = True

    context.user_data.update({
        "game_id": game_id,
        "game_name": game_name,
        "id_label": id_label,
        "requires_server": requires_server
    })

    markup = get_child_markup(context)
    kb = []
    skipped = 0

    for r in rows:
        try:
            sale = Decimal(str(r["sale_price"]))
            price = child_price(sale, markup)
            paket_id = int(r["paket_id"])
            package_name = str(r["package_name"] or "Paket")
        except Exception:
            skipped += 1
            continue

        kb.append([
            InlineKeyboardButton(
                f"📦 {package_name} — {price:,.0f} so'm",
                callback_data=f"o:{game_id}:{paket_id}"
            )
        ])

    if not kb:
        await q.message.reply_text("❌ Paketlar topilmadi.")
        return

    # Paketlarni uzun ID+nom+narx ro'yxati qilib alohida yubormaymiz.
    # Foydalanuvchi paketni tugmadan tanlaydi; keyin ID/username kiritadi.
    await q.message.reply_text(
        f"🎮 {game_name}\n\n"
        "📦 Paketni tanlang:",
        reply_markup=InlineKeyboardMarkup(kb)
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

    elif game_id in (PUBG_GAME_ID, PUBG_GLOBAL_GAME_ID):

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
            "❌ Paket hozir mavjud emas.\n\n"
            "Iltimos, keyinroq "
            "qayta urinib ko'ring."
        )

        return

    context.user_data.update({

        "game_id": game_id,

        "paket_id": paket_id,

        "offer_name": r[
            "package_name"
        ],

        "game_name": r[
            "game_name"
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

    game_name = context.user_data.get('game_name') or context.user_data.get('offer_game_name') or ''
    if not game_name:
        game_name = context.user_data.get('offer_name', 'O\'yin')

    player_name = str(context.user_data.get('player_name', '')).strip()

    await message.reply_text(
        f"🎮 {game_name}\n\n"
        f"📦 {context.user_data.get('offer_name','Paket')}\n"
        f"💰 Narx: {final_price:,.0f} so'm\n\n"
        f"🆔 {id_label}: {player_id}\n"
        + (f"👤 Nickname: {player_name}\n" if player_name else "")
        + extra
        + (f"🎁 Chegirma: {discount:,.0f} so'm\n" if discount else "")
        + "\n"
        "Tasdiqlaysizmi?",
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
# CONFIRM -> BAY2GAME
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

    # Nickname tekshiruvi mavjud bo'lmagan o'yinlarda ham buyurtma oqimi ishlaydi.
    # Muhimi: tekshirilmagan IDni nickname sifatida ko'rsatmaymiz.
    player_name = str(context.user_data.get("player_name", "")).strip()

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
        f"Bay2Game buyurtma: {game_id}/{paket_id}"
    )

    # ========================================================
    # BAY2GAME
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
            "Bay2Game API xatosi: "
            f"{data.get('error','unknown')}"
        )

        # Foydalanuvchiga provider/API yoki sayt nomi ko'rsatilmaydi.
        await q.message.reply_text(
            "❌ Xatolik\n\n"
            f"💰 Pul balansingizga qaytarildi: {price:,.0f} so'm",
            reply_markup=main_menu()
        )

        await notify_bot_owner_technical(
            context,
            uid,
            details=data.get("error") or data.get("message") or f"HTTP status: {status}",
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
        f"🔢 Buyurtma raqami: {playpay_id}\n"
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
            f"🔢 Buyurtma raqami: {playpay_id}\n"
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

    if state in ("withdraw_amount", "withdraw_details"):
        if await handle_withdrawal_text(update, context, state, text):
            return

    # ========================================================
    # FOYDALANUVCHILAR O'RTASIDA PUL O'TKAZISH
    # ========================================================
    if state == "transfer_recipient":
        try:
            recipient_id = int(text.strip())
        except Exception:
            await update.message.reply_text("❌ Telegram ID faqat raqam bo'lishi kerak. Masalan: 123456789")
            return
        if recipient_id <= 0 or recipient_id == u.id:
            await update.message.reply_text("❌ O'zingizga pul yubora olmaysiz. Boshqa foydalanuvchi ID sini kiriting.")
            return
        c = main_conn()
        try:
            recipient = c.execute("SELECT user_id, username, first_name FROM users WHERE user_id=?", (recipient_id,)).fetchone()
        finally:
            c.close()
        if recipient is None:
            await update.message.reply_text("❌ Foydalanuvchi topilmadi. U botni avval /start bilan ishga tushirsin.")
            return
        context.user_data["transfer_to"] = recipient_id
        context.user_data["state"] = "transfer_amount"
        name = recipient["first_name"] or "Foydalanuvchi"
        username = f" (@{recipient['username']})" if recipient["username"] else ""
        await update.message.reply_text(
            f"👤 Qabul qiluvchi: {name}{username}\n🆔 ID: {recipient_id}\n\n"
            "💸 Qancha pul o'tkazmoqchisiz? Summani so'mda yuboring.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")]])
        )
        return

    if state == "transfer_amount":
        try:
            amount = Decimal(text.replace(" ", "").replace(",", "").replace("so'm", ""))
        except Exception:
            await update.message.reply_text("❌ Summani raqamda yuboring. Masalan: 5000")
            return
        if amount != amount.to_integral_value() or amount < 1000 or amount > 1000000000:
            await update.message.reply_text("❌ O'tkazma summasi butun son bo'lishi va 1 000 dan 1 000 000 000 so'mgacha bo'lishi kerak.")
            return
        recipient_id = context.user_data.get("transfer_to")
        if not recipient_id or int(recipient_id) == u.id:
            context.user_data.clear()
            await update.message.reply_text("❌ Qabul qiluvchi ma'lumoti yo'q. Qaytadan boshlang.", reply_markup=main_menu())
            return
        balance = get_balance(u.id)
        if balance < amount:
            await update.message.reply_text(f"❌ Balansingiz yetarli emas.\n💰 Balans: {balance:,.0f} so'm\n💸 Kerak: {amount:,.0f} so'm", reply_markup=main_menu())
            context.user_data.clear()
            return
        c = main_conn()
        try:
            recipient = c.execute("SELECT username, first_name FROM users WHERE user_id=?", (int(recipient_id),)).fetchone()
        finally:
            c.close()
        if recipient is None:
            context.user_data.clear()
            await update.message.reply_text("❌ Qabul qiluvchi topilmadi. Qaytadan boshlang.", reply_markup=main_menu())
            return
        context.user_data["transfer_amount"] = int(amount)
        context.user_data["state"] = "transfer_confirm"
        recipient_name = recipient["first_name"] or "Foydalanuvchi"
        if recipient["username"]:
            recipient_name += f" (@{recipient['username']})"
        await update.message.reply_text(
            "🔎 <b>O'tkazmani tasdiqlang</b>\n\n"
            f"👤 Qabul qiluvchi: {recipient_name}\n"
            f"🆔 ID: <code>{int(recipient_id)}</code>\n"
            f"💸 Summa: <b>{amount:,.0f} so'm</b>\n"
            f"💰 Hozirgi balans: {balance:,.0f} so'm\n"
            f"💰 O'tkazmadan keyin: {balance - amount:,.0f} so'm",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Tasdiqlash", callback_data="transfer_confirm")],
                [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel")]
            ])
        )
        return

    # ========================================================
    # BAY2GAME: TELEGRAM STARS / PREMIUM
    # ========================================================
    if state == "ps_stars_username":
        name = text.strip().lstrip("@")
        if not name or " " in name or len(name) > 64:
            await update.message.reply_text("❌ Username noto'g'ri. Masalan: @username")
            return
        context.user_data["ps_username"] = name
        context.user_data["state"] = "ps_stars_quantity"
        await update.message.reply_text(
            "⭐ Nechta Stars kerak?\nMiqdorni yuboring. Paket Bay2Game katalogida bo'lishi shart."
        )
        return

    if state == "ps_stars_quantity":
        try:
            quantity = int(text.replace(" ", ""))
        except Exception:
            await update.message.reply_text("❌ Faqat son yuboring.")
            return
        if quantity < 1:
            await update.message.reply_text("❌ Miqdor musbat son bo'lishi kerak.")
            return
        try:
            product = await asyncio.to_thread(find_bay2game_telegram_product, "stars", quantity)
            if not product:
                await update.message.reply_text(
                    "❌ Bu miqdordagi Stars Bay2Game katalogida topilmadi.\n"
                    "Admin 🔄 Katalog tugmasini yangilasin yoki katalogdagi mavjud paketni tanlang."
                )
                return
            price = product["price"]
            context.user_data["ps_quantity"] = quantity
            context.user_data["ps_product_code"] = product["product_code"]
            context.user_data["ps_price"] = float(price)
            context.user_data["state"] = None
            await update.message.reply_text(
                f"⭐ Telegram Stars\n\n👤 @{context.user_data['ps_username']}\n"
                f"📦 {product['name']}\n💰 {price:,.0f} so'm\n"
                f"💳 Balans: {float(get_balance(u.id)):,.0f} so'm\n\nTasdiqlaysizmi?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data="ps_confirm_stars"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"),
                ]])
            )
        except Exception:
            log.exception("Bay2Game Stars catalog lookup failed")
            await update.message.reply_text("❌ Bay2Game katalogidan Stars narxini olishda xatolik.")
        return

    if state == "ps_premium_username":
        name = text.strip().lstrip("@")
        months = int(context.user_data.get("ps_months", 0) or 0)
        if not name or " " in name or len(name) > 64 or months not in (3, 6, 12):
            await update.message.reply_text("❌ Username yoki Premium muddati noto'g'ri.")
            return
        try:
            product = await asyncio.to_thread(find_bay2game_telegram_product, "premium", months)
            if not product:
                await update.message.reply_text(
                    f"❌ Telegram Premium {months} oy Bay2Game katalogida topilmadi.\n"
                    "Admin 🔄 Katalog tugmasini yangilasin yoki Bay2Game katalogini tekshirsin."
                )
                return
            context.user_data["ps_username"] = name
            context.user_data["ps_product_code"] = product["product_code"]
            context.user_data["ps_price"] = float(product["price"])
            context.user_data["state"] = None
            await update.message.reply_text(
                f"💎 Telegram Premium\n\n👤 @{name}\n📦 {product['name']}\n"
                f"💰 {product['price']:,.0f} so'm\n"
                f"💳 Balans: {float(get_balance(u.id)):,.0f} so'm\n\nTasdiqlaysizmi?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data="ps_confirm_premium"),
                    InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"),
                ]])
            )
        except Exception:
            log.exception("Bay2Game Premium catalog lookup failed")
            await update.message.reply_text("❌ Bay2Game katalogidan Premium narxini olishda xatolik.")
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
                "♾️ Obunasiz — doimiy ishlaydi.",
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
    # OMONAT SUMMASI
    # ========================================================

    if state == "omonat_amount":
        try:
            amount = Decimal(text.replace(",", "").replace(" ", ""))
        except InvalidOperation:
            await update.message.reply_text("❌ Summani raqamda yuboring. Masalan: 50000")
            return
        if amount <= 0 or amount != amount.to_integral_value():
            await update.message.reply_text("❌ Summani musbat butun so'mda yuboring.")
            return
        amount = amount.quantize(Decimal("1"))
        uid = u.id
        balance = get_balance(uid)
        if balance < amount:
            await update.message.reply_text(
                f"❌ Balans yetarli emas.\n💰 Kerak: {amount:,.0f} so'm\n💳 Balans: {balance:,.0f} so'm"
            )
            return
        started = datetime.now()
        matures = add_one_calendar_month(started)
        profit = (amount * Decimal("0.01")).quantize(Decimal("1"))
        c = main_conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            user_row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
            if not user_row or Decimal(str(user_row["balance"] or 0)) < amount:
                c.rollback()
                await update.message.reply_text("❌ Balans yetarli emas. Balansingizni tekshirib qayta urinib ko'ring.")
                return
            c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (float(amount), uid))
            c.execute("INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)", (uid, -float(amount), "omonat_open", "Omonat ochildi", started.isoformat()))
            cur = c.execute("INSERT INTO omonats(user_id,principal,profit,status,started_at,matures_at) VALUES(?,?,?,'active',?,?)", (uid, float(amount), float(profit), started.isoformat(), matures.isoformat()))
            omonat_id = cur.lastrowid
            c.commit()
        except Exception:
            c.rollback()
            log.exception("Omonat ochishda xatolik")
            await update.message.reply_text("❌ Omonat ochilmadi. Qayta urinib ko'ring.")
            return
        finally:
            c.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Omonat #{omonat_id} ochildi!\n\n"
            f"💰 Summa: {amount:,.0f} so'm\n"
            f"📈 1% foyda: {profit:,.0f} so'm\n"
            f"💵 Muddat tugaganda jami: {amount + profit:,.0f} so'm\n"
            f"🗓 Ochilgan: {started.strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"⏰ Tugash vaqti: {matures.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            "Muddat tugaganda summa va foyda balansingizga avtomatik qaytariladi.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 Omonat tarixi", callback_data="omonat_history")],
                [InlineKeyboardButton("🏦 Omonat menyusi", callback_data="omonat")]
            ])
        )
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
    # GRAND MOBILE ID
    # ========================================================
    if state == "grand_player_id":
        if not text or len(text) > 100:
            await update.message.reply_text("❌ Grand Mobile ID noto'g'ri.")
            return

        context.user_data["grand_player_id"] = text
        gc = int(context.user_data.get("grand_gc", 0))
        price = Decimal(str(context.user_data.get("grand_price", 0)))
        balance = Decimal(str(get_balance(u.id)))
        context.user_data["state"] = None

        await update.message.reply_text(
            f"🚗 Grand Mobile\n\n"
            f"📦 {gc} GC\n"
            f"💰 {price:,.0f} so'm\n"
            f"🆔 Grand Mobile ID: {text}\n\n"
            f"💳 Balans: {balance:,.0f} so'm\n\n"
            "⚠️ Manual buyurtma admin tomonidan bajariladi.\n"
            "Tasdiqlaysizmi?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Tasdiqlash", callback_data="grand_confirm"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel"),
            ]])
        )
        return

    # ========================================================
    # PLAYER / USER ID
    # ========================================================

    async def validate_game_id():

        game_id = context.user_data.get("game_id")
        player_id = str(
            context.user_data.get("player_id", "")
        ).strip()
        server_id = str(
            context.user_data.get("server_id", "")
        ).strip()

        if not BAY2GAME_API_KEY:
            await update.message.reply_text(
                "❌ Xizmat vaqtincha sozlanmagan. Iltimos, keyinroq qayta urinib ko'ring."
            )
            context.user_data["state"] = None
            return

        await update.message.reply_text(
            "🔎 ID tekshirilmoqda..."
        )

        status, data = await asyncio.to_thread(
            playpay_check_id,
            game_id,
            player_id,
            server_id if server_id else None
        )

        if not isinstance(data, dict):
            await update.message.reply_text(
                "❌ Tekshiruv vaqtida xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring."
            )
            context.user_data["state"] = None
            return

        if data.get("ok") is False:
            error = str(data.get("error") or data.get("message") or "Tekshiruv xatosi")
            await update.message.reply_text(
                f"❌ ID tekshirilmadi.\n\n{error}"
            )
            context.user_data["state"] = None
            return

        # Agar API haqiqiy nickname tekshiruvini qo'llamasa, IDni nickname sifatida
        # ko'rsatmaymiz va foydalanuvchiga tekshiruv mavjud emasligini ochiq aytamiz.
        if data.get("validation_supported") is False:
            context.user_data.pop("player_name", None)
            context.user_data["state"] = None
            await update.message.reply_text(
                "⚠️ ID formati qabul qilindi, lekin hisob egasining nickname'ini "
                "oldindan tekshirish hozircha mavjud emas.\n"
                "Buyurtmani tasdiqlashdan oldin ID to'g'riligini o'zingiz tekshiring."
            )
            await confirm_order(update.message, context)
            return

        valid = data.get("valid")
        if valid is None:
            valid = data.get("success")
        if valid is None:
            valid = data.get("is_valid")

        if not bool(valid):
            await update.message.reply_text(
                "❌ ID noto'g'ri yoki o'yinchi topilmadi.\n\n"
                "🆔 ID ni tekshirib, qaytadan buyurtma bering."
            )
            context.user_data["state"] = None
            context.user_data.pop("player_name", None)
            return

        player_name = str(
            data.get("player_name")
            or data.get("nickname")
            or data.get("name")
            or data.get("username")
            or ""
        ).strip()

        context.user_data["player_name"] = player_name
        context.user_data["state"] = None

        await update.message.reply_text(
            "✅ ID qabul qilindi.\n\n"
            f"🆔 ID: <code>{html.escape(player_id)}</code>",
            parse_mode="HTML"
        )

        await confirm_order(
            update.message,
            context
        )

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

        # Server kerak bo'lmasa, ID'ni hozir tekshiramiz.
        if not context.user_data.get("requires_server", False):
            await validate_game_id()
            return

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

        await validate_game_id()

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
            f"🔢 Bay2Game: "
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
        reply_markup=admin_kb(context)
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
            f"🔢 Bay2Game: "
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


async def admin_markup_start(update, context, kind):
    q = update.callback_query
    if q.from_user.id != bot_admin_id(context):
        await q.answer("Siz admin emassiz.", show_alert=True)
        return
    labels = {
        "donat": ("🎮 Donat", "donat_markup_percent", DEFAULT_MARKUP),
        "sim": ("📱 SIM", "sim_markup_percent", SIM_MARKUP_PERCENT),
        "stars": ("⭐ Stars", "stars_markup_percent", PAYSTARS_MARKUP_PERCENT),
        "premium": ("💎 Premium", "premium_markup_percent", PAYSTARS_MARKUP_PERCENT),
    }
    label, key, default = labels[kind]
    try:
        current = Decimal(str(get_setting(key, default)))
    except Exception:
        current = Decimal(str(default))
    context.user_data.clear()
    context.user_data["admin_state"] = f"markup_{kind}"
    await q.message.reply_text(
        f"{label} ustama sozlamasi\n\n"
        f"📈 Hozirgi ustama: {current:g}%\n"
        "💱 Narx UZS da hisoblanadi.\n\n"
        "Yangi ustama foizini yuboring.\n"
        "Masalan: 10\n"
        "0 = ustamasiz"
    )


async def admin_paystars_price_start(update, context):
    await admin_markup_start(update, context, "stars")

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
    # BAY2GAME USD KURSI
    # ========================================================
    if state == "bay2game_usd_uzs":
        try:
            rate = Decimal(text.replace(" ", "").replace(",", ".").replace("so'm", ""))
        except Exception:
            await update.message.reply_text("❌ Kursni raqamda yuboring. Masalan: 13000")
            return True
        if rate < 1 or rate > 100000000:
            await update.message.reply_text("❌ Kurs 1 dan 100 000 000 so'mgacha bo'lishi kerak.")
            return True
        set_setting("bay2game_usd_uzs", str(rate))
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Bay2Game USD kursi saqlandi: 1 USD = {rate:,.2f} so'm\n"
            "🔄 Katalog va paket narxlari yangilanmoqda..."
        )
        ok, result = await asyncio.to_thread(sync_catalog)
        if ok:
            await asyncio.to_thread(sync_all_child_catalogs)
        await update.message.reply_text(
            f"{'✅' if ok else '⚠️'} {result}\n\n"
            "Yangi kurs katalogdagi Bay2Game paketlariga qo'llandi.",
            reply_markup=admin_kb(context)
        )
        return True

    # ========================================================
    # REFERAL BONUSI
    # ========================================================
    if state == "referral_bonus":
        try:
            bonus = Decimal(text.replace(" ", "").replace(",", ""))
        except Exception:
            await update.message.reply_text("❌ Bonusni raqamda yuboring. Masalan: 100")
            return True
        if bonus < 0 or bonus > 100000000:
            await update.message.reply_text("❌ Bonus 0 dan 100 000 000 so'mgacha bo'lishi kerak.")
            return True
        set_setting("referral_bonus_uzs", str(int(bonus)))
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Referal bonusi saqlandi: {int(bonus):,} so'm\n"
            "Bu qiymat keyingi yangi referallarga qo'llanadi.",
            reply_markup=admin_kb(context)
        )
        return True

    # ========================================================
    # ALOHIDA USTAMALAR
    # ========================================================
    if state in ("markup_donat", "markup_sim", "markup_stars", "markup_premium"):
        try:
            percent = Decimal(text.replace(",", ".").replace("%", "").strip())
        except Exception:
            await update.message.reply_text("❌ Foizni raqamda yuboring. Masalan: 10")
            return True
        if percent < 0 or percent > 1000:
            await update.message.reply_text("❌ Foiz 0 dan 1000 gacha bo'lishi kerak.")
            return True
        kind = state.replace("markup_", "")
        keys = {"donat":"donat_markup_percent", "sim":"sim_markup_percent", "stars":"stars_markup_percent", "premium":"premium_markup_percent"}
        labels = {"donat":"🎮 Donat", "sim":"📱 SIM / Virtual raqam", "stars":"⭐ Stars", "premium":"💎 Premium"}
        set_setting(keys[kind], str(percent))
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ {labels[kind]} ustamasi saqlandi: {percent:g}%\n\n"
            "Bu xizmatning ustamasi boshqalardan alohida.",
            reply_markup=admin_kb(context)
        )
        return True

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
            reply_markup=admin_kb(context)
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
            reply_markup=admin_kb(context)
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
            reply_markup=admin_kb(context)
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
            reply_markup=admin_kb(context)
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
            reply_markup=admin_kb(context)
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

            sent, failed = await send_channel_post(
                context,
                text=context.user_data.get("post_text", "")
            )

            context.user_data.clear()

            await update.message.reply_text(
                f"📣 Yuborish yakunlandi.\n\n✅ Yetkazildi: {sent}\n❌ Yuborilmadi: {failed}",
                reply_markup=admin_kb(context)
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
    """Admin postini kanalga emas, shu botdagi foydalanuvchilarning shaxsiy chatlariga yuboradi."""
    c = conn()
    try:
        rows = c.execute("SELECT user_id FROM users WHERE user_id IS NOT NULL").fetchall()
        user_ids = [int(row["user_id"]) for row in rows]
    finally:
        c.close()

    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=user_id, photo=photo_id, caption=text or None)
            elif animation_id:
                await context.bot.send_animation(chat_id=user_id, animation=animation_id, caption=text or None)
            elif video_id:
                await context.bot.send_video(chat_id=user_id, video=video_id, caption=text or None)
            else:
                await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except Exception as exc:
            failed += 1
            # Foydalanuvchi xabari yoki bot tokeni logga chiqarilmaydi.
            log.info("Broadcast yuborilmadi: user_id=%s, error_type=%s", user_id, type(exc).__name__)

    return sent, failed


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

            sent, failed = await send_channel_post(
                context,
                text=caption,
                photo_id=update.message.photo[-1].file_id
            )

        elif update.message.animation:

            sent, failed = await send_channel_post(
                context,
                text=caption,
                animation_id=update.message.animation.file_id
            )

        elif update.message.video:

            sent, failed = await send_channel_post(
                context,
                text=caption,
                video_id=update.message.video.file_id
            )

        else:

            return

        context.user_data.clear()

        await update.message.reply_text(
            f"📣 Yuborish yakunlandi.\n\n✅ Yetkazildi: {sent}\n❌ Yuborilmadi: {failed}",
            reply_markup=admin_kb(context)
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

    platform_only_callbacks = {"adm_bay2game_rate", "adm_playpay_balance", "adm_paystars_balance", "a_sync"}
    if d in platform_only_callbacks and (is_child_bot(context) or q.from_user.id != ADMIN_ID):
        await q.answer("Xatolik", show_alert=True)
        return

    if d.startswith("adm_wd_ok_") or d.startswith("adm_wd_no_"):
        if q.from_user.id != ADMIN_ID:
            return await q.message.reply_text("❌ Bu amal faqat platforma admini uchun.")
        try:
            request_id = int(d.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return await q.message.reply_text("❌ So'rov raqami noto'g'ri.")
        approve = d.startswith("adm_wd_ok_")
        c = main_conn()
        row = None
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM withdrawals WHERE id=?", (request_id,)).fetchone()
            if not row or row["status"] != "pending":
                c.rollback()
                return await q.message.reply_text("ℹ️ Bu so'rov allaqachon ko'rib chiqilgan yoki topilmadi.")
            now = datetime.now().isoformat()
            new_status = "approved" if approve else "rejected"
            c.execute("UPDATE withdrawals SET status=?, decided_at=? WHERE id=? AND status='pending'", (new_status, now, request_id))
            if not approve:
                amount = float(row["amount"])
                c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, row["user_id"]))
                c.execute(
                    "INSERT INTO balance_history(user_id,amount,type,note,created_at) VALUES(?,?,?,?,?)",
                    (row["user_id"], amount, "withdrawal_refund", f"Yechish so'rovi #{request_id} rad etildi", now),
                )
            c.commit()
        except Exception:
            c.rollback()
            log.exception("Yechish so'rovini ko'rib chiqishda xato")
            return await q.message.reply_text("❌ So'rovni yangilashda xatolik.")
        finally:
            c.close()
        result_text = (f"✅ #{request_id} so'rov to'langan deb belgilandi." if approve else f"❌ #{request_id} so'rov rad etildi, pul foydalanuvchi balansiga qaytarildi.")
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"✅ Balans yechish so'rovingiz #{request_id} tasdiqlandi. Admin to'lovni amalga oshirgan bo'lishi kerak."
                    if approve else
                    f"❌ Balans yechish so'rovingiz #{request_id} rad etildi. {float(row['amount']):,.0f} so'm balansingizga qaytarildi."
                ),
            )
        except Exception:
            pass
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await q.message.reply_text(result_text)

    if d == "adm_referral_bonus":
        context.user_data.clear()
        context.user_data["admin_state"] = "referral_bonus"
        await q.message.reply_text(
            "🔗 Referal bonusi sozlamasi\n\n"
            f"Hozirgi bonus: {get_setting('referral_bonus_uzs', '100')} so'm\n"
            "Har bir yangi taklif uchun bonus summasini yuboring.\n"
            "Masalan: 100"
        )

    elif d == "adm_addbalance":

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

    elif d == "adm_paystars_price":
        await admin_paystars_price_start(update, context)

    elif d == "adm_markup_donat":
        await admin_markup_start(update, context, "donat")

    elif d == "adm_markup_sim":
        await admin_markup_start(update, context, "sim")

    elif d == "adm_markup_stars":
        await admin_markup_start(update, context, "stars")

    elif d == "adm_markup_premium":
        await admin_markup_start(update, context, "premium")

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

    elif d == "adm_bay2game_rate":
        if q.from_user.id != bot_admin_id(context):
            await q.answer("Siz admin emassiz.", show_alert=True)
            return
        current_rate = get_bay2game_usd_uzs()
        context.user_data.clear()
        context.user_data["admin_state"] = "bay2game_usd_uzs"
        await q.message.reply_text(
            "💱 BAY2GAME USD KURSI\n\n"
            f"Hozirgi kurs: 1 USD = {current_rate:,.2f} so'm\n\n"
            "Yangi kursni so'mda yuboring.\n"
            "Masalan: 13000\n\n"
            "Kurs saqlangach katalogdagi paketlar narxi avtomatik yangilanadi."
        )

    elif d == "adm_playpay_balance":

        status, data = await asyncio.to_thread(
            get_playpay_balance
        )

        if data.get("ok"):

            b = data.get("balance", {})
            if not isinstance(b, dict):
                b = {"amount": b, "currency": "USD"}
            profile = data.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}
            # Ba'zi Bay2Game javoblarida maydonlar profile ichida, boshqalarida root darajada keladi.
            raw = data.get("raw", {}) if isinstance(data.get("raw"), dict) else {}
            def pval(*keys, default="—"):
                for source in (profile, raw):
                    for key in keys:
                        value = source.get(key)
                        if value not in (None, ""):
                            return value
                return default
            amount = b.get('amount', b.get('usd', '0'))
            currency = b.get('currency', 'USD')
            telegram_id = pval("telegram_id", "user_id", "id")
            username = pval("username", "user_name")
            role = pval("role", default="reseller")
            profile_balance = pval("balance", "balance_usd", "api_balance", default=amount)
            if isinstance(profile_balance, dict):
                profile_balance = profile_balance.get("amount", profile_balance.get("usd", amount))
            profile_balance_text = str(profile_balance)
            if "USD" not in profile_balance_text.upper() and not profile_balance_text.startswith("$"):
                profile_balance_text = f"${profile_balance_text} USD"
            elif not profile_balance_text.startswith("$"):
                profile_balance_text = "$" + profile_balance_text
            total_orders = pval("total_orders", "orders_count", "orders", default=0)
            total_spent = pval("total_spent", "spent", "spent_usd", default=0)
            spent_text = str(total_spent)
            if "USD" not in spent_text.upper() and not spent_text.startswith("$"):
                spent_text = f"${spent_text}"
            elif not spent_text.startswith("$"):
                spent_text = "$" + spent_text
            joined = pval("joined", "joined_at", "created_at", default="—")
            profile_status = str(pval("status", default="active")).lower()
            status_text = "✅ Active" if profile_status in ("active", "enabled", "ok", "true", "1") else f"⚠️ {profile_status.title()}"
            profile_text = (
                "\n\n👤 My Profile\n\n"
                f"┌ 🆔 Telegram ID: {telegram_id}\n"
                f"├ 📝 Username: {('@' + str(username).lstrip('@')) if username != '—' else '—'}\n"
                f"├ 👑 Role: {role}\n"
                f"├ 💰 Balance: {profile_balance_text}\n"
                f"├ 📊 Status: {status_text}\n"
                f"├ 🛍️ Total Orders: {total_orders}\n"
                f"├ 💸 Total Spent: {spent_text}\n"
                f"└ 📅 Joined: {joined}"
            )
            await q.message.reply_text(
                f"🔐 BAY2GAME BALANSI\n\n"
                f"💵 USD: {amount}\n"
                f"💱 Valyuta: {currency}\n"
                f"💱 Kurs: 1 USD = {get_bay2game_usd_uzs():,.0f} so'm"
                f"{profile_text}\n\n"
                "✨ Xizmat balansini boshqarish uchun administrator bilan bog'laning.\n"
                "💡 Yordam kerak bo'lsa, qo'llab-quvvatlashga yozing."
            )

        else:

            await q.message.reply_text("❌ Xatolik")

    # ========================================================
    # FAQAT ADMIN KATALOG YANGILAYDI
    # ========================================================

    elif d == "a_sync":

        await q.message.reply_text(
            "🔄 Bay2Game katalogi yangilanmoqda..."
        )

        ok, result = await asyncio.to_thread(
            sync_catalog
        )
        if ok:
            sync_all_child_catalogs()

        await q.message.reply_text(
            f"{'✅' if ok else '❌'} {result}",
            reply_markup=admin_kb(context)
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

    # Foydalanuvchilar o'rtasida pul o'tkazish
    if d == "transfer_start":
        ensure_user(q.from_user)
        return await transfer_start(update, context)
    if d == "transfer_confirm":
        return await transfer_confirm(update, context)

    # Omonat menyusi va tarixi
    if d == "omonat":
        return await omonat_menu(update, context)
    if d == "omonat_open":
        return await omonat_open_start(update, context)
    if d == "omonat_history":
        return await omonat_history(update, context)
    if d.startswith("omonat_close_yes_"):
        return await omonat_close_confirm(update, context, int(d.rsplit("_", 1)[1]))
    if d.startswith("omonat_close_"):
        return await omonat_close_prompt(update, context, int(d.rsplit("_", 1)[1]))

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
    if d == "adm_paystars_balance":
        return await paystars_balance_admin(update, context)
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
        if q.from_user.id==ADMIN_ID and r: await start_child_bot(bot_id)
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
    if d == "grand_mobile":
        return await grand_mobile(update, context)
    if d.startswith("grand:"):
        return await grand_package(update, context)
    if d == "grand_confirm":
        return await grand_confirm(update, context)

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

        elif d == "referral":
            await referral_menu(update, context)

        elif d == "withdraw":
            await withdrawal_start(update, context)

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

    user = update.effective_user
    if user is None:
        return

    if user.id == bot_admin_id(context):

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
            admin_kb(context)
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

    # Eski PlayPay katalogi Bay2Game product_code bo'lmagan holda sotuvga chiqmasin.
    # Bay2Game katalogi admin panelidagi "Katalog" tugmasi orqali yangilanadi.
    c = conn()
    try:
        c.execute("UPDATE products SET active=0 WHERE COALESCE(provider_product_code,'')=''")
        c.execute("UPDATE games SET active=0 WHERE COALESCE(provider_game_code,'')=''")
        c.commit()
    finally:
        c.close()

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

    if not BAY2GAME_API_KEY:

        raise SystemExit(
            "❌ BAY2GAME_API_KEY Environment Variable yozilmagan."
        )

    if Fernet is None:
        raise SystemExit("❌ cryptography o'rnatilmagan. requirements.txt ga cryptography qo'shing.")

    # Omonat muddatlarini fon rejimida tekshirish (har 30 soniyada).
    threading.Thread(target=omonat_worker, daemon=True, name="omonat-worker").start()

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
        "       DONUZ BAY2GAME BOT"
    )

    print("[BOT] Telegram bot ishga tushmoqda")
    print("[WEB] HTTP health server / yo'lida tayyor")

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
        "       MARKUP: 0% | WEB API: ON"
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

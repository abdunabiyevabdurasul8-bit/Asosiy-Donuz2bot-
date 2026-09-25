import sqlite3
import logging
import uuid
import json
import os
import asyncio
import threading

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

# SQLite
DB = "bot.db"

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
        DB,
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
    """)

    c.commit()
    c.close()

    set_default(
        "payment_card",
        PAYMENT_CARD
    )


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
# FAQAT ADMIN ðŸ”„ KATALOG ORQALI ISHLAYDI
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
            f"âœ… {game_count} ta o'yin, "
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
        f"âœ… {game_count} ta o'yin, "
        f"{package_count} ta paket yangilandi."
    )


# ============================================================
# MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "ðŸ›’ Buyurtma berish",
                callback_data="games"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ’³ Balans to'ldirish",
                callback_data="deposit"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ“¦ Buyurtmalarim",
                callback_data="orders"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸŽ Promo kod",
                callback_data="promo"
            ),
            InlineKeyboardButton(
                "ðŸ‘¤ Profil",
                callback_data="profile"
            )
        ]
    ])


def admin_kb():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "ðŸ’° Balans + / -",
                callback_data="adm_addbalance"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ’³ To'lovlar",
                callback_data="adm_payments"
            ),
            InlineKeyboardButton(
                "ðŸ“Š Statistika",
                callback_data="adm_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ† Reyting",
                callback_data="adm_rating"
            ),
            InlineKeyboardButton(
                "ðŸ‘¤ Foydalanuvchilar",
                callback_data="adm_users"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ“¦ Buyurtmalar",
                callback_data="adm_orders"
            ),
            InlineKeyboardButton(
                "ðŸ’µ Narxlar",
                callback_data="adm_prices"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸŽ Promo",
                callback_data="adm_promo"
            ),
            InlineKeyboardButton(
                "ðŸ’³ Karta",
                callback_data="adm_card"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ“¢ Post",
                callback_data="adm_post"
            ),
            InlineKeyboardButton(
                "ðŸ”„ Katalog",
                callback_data="a_sync"
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ” PlayPay balansi",
                callback_data="adm_playpay_balance"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):

    ensure_user(
        update.effective_user
    )

    if not update.message:
        return

    await update.message.reply_text(
        "Assalomu Aleykum ðŸ‘‹\n\n"
        "ðŸŽ® Donat botiga xush kelibsiz!",
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
            "âŒ O'yinlar topilmadi.\n\n"
            "ðŸ‘‘ Admin paneldan ðŸ”„ Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    kb = []

    for r in rows:

        kb.append([
            InlineKeyboardButton(
                "ðŸŽ® " + r["name"],
                callback_data=f"g:{r['game_id']}"
            )
        ])

    await q.message.reply_text(
        "ðŸŽ® O'yinni tanlang:",
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
            "âŒ O'yin ID xato."
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
            "âŒ O'yin topilmadi.\n\n"
            "ðŸ‘‘ Admin paneldan ðŸ”„ Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    if not rows:

        await q.message.reply_text(
            f"âŒ {g['name']} uchun paketlar topilmadi.\n\n"
            "ðŸ‘‘ Admin paneldan ðŸ”„ Katalog "
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
                f"{r['package_name']} â€” "
                f"{sale:,.0f} so'm",
                callback_data=(
                    f"o:{game_id}:{r['paket_id']}"
                )
            )
        ])

    if not kb:

        await q.message.reply_text(
            "âŒ Paketlar topilmadi."
        )

        return

    await q.message.reply_text(
        f"ðŸ“¦ {game_name}\n\n"
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
            "âŒ Paket ID xato."
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
            "âŒ Paket katalogda topilmadi.\n\n"
            "ðŸ‘‘ Admin paneldan ðŸ”„ Katalog "
            "tugmasini bosib katalogni yangilang."
        )

        return

    context.user_data.update({

        "game_id": game_id,

        "paket_id": paket_id,

        "offer_name": r[
            "package_name"
        ],

        "price": Decimal(
            str(
                r["sale_price"]
            )
        ),

        "id_label": id_label,

        "requires_server": requires_server,

        "state": "player_id"
    })

    await q.message.reply_text(
        f"ðŸŽ® {r['game_name']}\n"
        f"ðŸ“¦ {r['package_name']}\n"
        f"ðŸ’° Narx: "
        f"{Decimal(str(r['sale_price'])):,.0f} so'm\n\n"
        f"ðŸ†” {id_label} ni yuboring:\n\n"
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
            f"ðŸŒ Server ID: {server_id}\n"
        )

    await message.reply_text(
        f"ðŸ“¦ {context.user_data.get('offer_name','Paket')}\n\n"
        f"ðŸ†” {id_label}: {player_id}\n"
        f"{extra}"
        f"ðŸ’° Narx: {final_price:,.0f} so'm\n"
        +
        (
            f"ðŸŽ Chegirma: "
            f"{discount:,.0f} so'm\n"
            if discount
            else ""
        )
        +
        "\nBuyurtmani tasdiqlaysizmi?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "âœ… Tasdiqlash",
                    callback_data="confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    "âŒ Bekor qilish",
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
            "âŒ Buyurtma narxi xato."
        )

        return

    current = get_balance(uid)

    if current < price:

        await q.message.reply_text(
            "âŒ Balans yetarli emas.\n\n"
            f"ðŸ’° Balans: {current:,.0f} so'm\n"
            f"ðŸ’µ Kerak: {price:,.0f} so'm",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "ðŸ’³ Balans to'ldirish",
                        callback_data="deposit"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "âŒ Bekor qilish",
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
            "âŒ Player/User ID kiritilmagan."
        )

        return

    if requires_server and not server_id:

        await q.message.reply_text(
            "âŒ Server ID kiritilmagan."
        )

        return

    if game_id is None or paket_id is None:

        await q.message.reply_text(
            "âŒ Buyurtma ma'lumotlari topilmadi."
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
            "âŒ Buyurtma yuborilmadi.\n\n"
            f"Xato: {err}\n\n"
            f"ðŸ’° Pul balansga qaytarildi: "
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
            f"ðŸŒ Server ID: {server_id}\n"
        )

    await q.message.reply_text(
        f"âœ… Buyurtma yuborildi!\n\n"
        f"ðŸ“¦ {context.user_data.get('offer_name','Paket')}\n"
        f"ðŸ†” {id_label}: {player_id}\n"
        f"{user_extra}"
        f"ðŸ’° {price:,.0f} so'm\n"
        f"ðŸ”¢ PlayPay order: {playpay_id}\n"
        f"ðŸ“Š Status: {order_status}",
        reply_markup=main_menu()
    )

    # ========================================================
    # ADMIN
    # ========================================================

    try:

        await context.bot.send_message(
            ADMIN_ID,
            f"ðŸ›’ YANGI BUYURTMA #{local_order_id}\n\n"
            f"ðŸ‘¤ User ID: {uid}\n"
            f"ðŸŽ® Game ID: {game_id}\n"
            f"ðŸ“¦ {context.user_data.get('offer_name','Paket')}\n"
            f"ðŸ†” {id_label}: {player_id}\n"
            +
            (
                f"ðŸŒ Server ID: {server_id}\n"
                if requires_server
                else ""
            )
            +
            f"ðŸ’° Sotuv: {price:,.0f} so'm\n"
            f"ðŸ”¢ PlayPay ID: {playpay_id}\n"
            f"ðŸ“Š {order_status}\n"
            f"ðŸ’µ API charged: {charged_usd} USD"
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
        "âŒ Bekor qilindi.",
        reply_markup=main_menu()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance_cb(update, context):

    q = update.callback_query

    await q.message.reply_text(
        "ðŸ’° Balansingiz:\n\n"
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
        "ðŸ’³ Balans to'ldirish\n\n"
        f"Karta: `{card}`\n\n"
        "Qancha pul tashlamoqchisiz?\n"
        "Masalan: 50000",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "âŒ Bekor qilish",
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
                "âŒ Summani raqamda yuboring."
            )

            return

        if amount <= 0:

            await update.message.reply_text(
                "âŒ Noto'g'ri summa."
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
            f"ðŸ’³ To'lov kartasi:\n\n"
            f"`{card}`\n\n"
            f"ðŸ’° Tashlaydigan summa: "
            f"{amount:,.0f} so'm\n\n"
            "âš ï¸ Aynan shu summani tashlang.\n"
            "To'lovdan keyin ðŸ“¸ chek rasmini yuboring.\n\n"
            "Chek admin tomonidan tekshiriladi.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "âŒ Bekor qilish",
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
                "âŒ Promo kod noto'g'ri."
            )

            return

        if (
            r["max_uses"] > 0
            and
            r["used"] >= r["max_uses"]
        ):

            await update.message.reply_text(
                "âŒ Promo kodi limiti tugagan."
            )

            return

        if used:

            await update.message.reply_text(
                "âŒ Bu promo koddan oldin foydalangansiz."
            )

            return

        context.user_data[
            "promo_code"
        ] = code

        context.user_data[
            "state"
        ] = None

        await update.message.reply_text(
            f"âœ… {code} qabul qilindi!\n"
            f"ðŸŽ Chegirma: {r['percent']}%"
        )

        return

    # ========================================================
    # PLAYER / USER ID
    # ========================================================

    if state == "player_id":

        if len(text) > 100:

            await update.message.reply_text(
                "âŒ ID juda uzun."
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
                "ðŸŒ Server ID ni yuboring:\n\n"
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
                "âŒ Server ID juda uzun."
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
            "âŒ Summa xatosi."
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
        "âœ… Chek adminga yuborildi.\n\n"
        "Admin tekshirganidan keyin balansingizga "
        "tasdiqlangan summa qo'shiladi."
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "âœ… Qabul qilish",
                callback_data=f"payok:{pid}"
            ),
            InlineKeyboardButton(
                "âŒ Rad etish",
                callback_data=f"payno:{pid}"
            )
        ]
    ])

    await context.bot.send_photo(
        ADMIN_ID,
        photo_id,
        caption=(
            f"ðŸ’³ TO'LOV #{pid}\n\n"
            f"ðŸ‘¤ User ID: {u.id}\n"
            f"ðŸ‘¤ @{u.username or 'username yoâ€˜q'}\n"
            f"ðŸ’° So'ralgan: "
            f"{amount:,.0f} so'm\n"
            f"ðŸ• "
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

    if q.from_user.id != ADMIN_ID:

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
            "âŒ To'lov ID xato."
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
            "âŒ To'lov topilmadi."
        )

        return

    if payment["status"] != "pending":

        await q.message.reply_text(
            "âš ï¸ Bu to'lov allaqachon ko'rilgan."
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
            "âŒ To'lovingiz admin tomonidan rad etildi."
        )

        await q.message.reply_text(
            "âŒ To'lov rad etildi."
        )

        return

    context.user_data[
        "admin_state"
    ] = "approve_payment"

    context.user_data[
        "payment_id"
    ] = pid

    await q.message.reply_text(
        f"ðŸ’³ To'lov #{pid}\n\n"
        f"ðŸ‘¤ User: {payment['user_id']}\n"
        f"ðŸ’° So'ralgan: "
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
        f"ðŸ‘¤ PROFIL\n\n"
        f"ðŸ†” ID: {r['user_id']}\n"
        f"ðŸ‘¤ Username: @{r['username'] or 'yoâ€˜q'}\n"
        f"ðŸ’° Balans: {r['balance']:,.0f} so'm\n"
        f"ðŸ“¦ Buyurtmalar: {orders}\n"
        f"ðŸ“… Sana: {created.strftime('%d.%m.%Y')}\n"
        f"â° Vaqt: {created.strftime('%H:%M')}",
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
            "ðŸ“¦ Buyurtmalar yo'q."
        )

        return

    text = "ðŸ“¦ BUYURTMALARIM\n\n"

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
            f"#{r['id']} â€” "
            f"{r['product_name']}\n"
            f"ðŸ†” {r['player_id']}\n"
        )

        if server:

            text += (
                f"ðŸŒ Server ID: {server}\n"
            )

        text += (
            f"ðŸ’° {r['sale_price']:,.0f} so'm\n"
            f"ðŸ“Š {r['status']}\n"
            f"ðŸ”¢ PlayPay: "
            f"{r['playpay_order_id']}\n"
            f"ðŸ• {r['created_at']}\n\n"
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
        "ðŸŽ Promo kodni yuboring:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "âŒ Bekor qilish",
                    callback_data="cancel"
                )
            ]
        ])
    )


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(update, context):

    if update.effective_user.id != ADMIN_ID:

        await update.message.reply_text(
            "âŒ Siz admin emassiz."
        )

        return

    await update.message.reply_text(
        "ðŸ‘‘ ADMIN PANEL",
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
        "ðŸ“Š ADMIN STATISTIKA\n\n"
        "ðŸŸ¢ BUGUN\n"
        f"ðŸ’µ Kirim: {today_income:,.0f} so'm\n"
        f"ðŸ”´ Chiqim: {today_out:,.0f} so'm\n"
        f"ðŸ‘¥ Yangi user: {today_users}\n"
        f"ðŸ“¦ Buyurtma: {today_orders}\n\n"
        "ðŸŸ¡ KECHA\n"
        f"ðŸ’µ Kirim: {yesterday_income:,.0f} so'm\n"
        f"ðŸ”´ Chiqim: {yesterday_out:,.0f} so'm\n\n"
        "ðŸ”µ 1 HAFTA\n"
        f"ðŸ’µ Kirim: {week_income:,.0f} so'm\n"
        f"ðŸ”´ Chiqim: {week_out:,.0f} so'm\n"
        f"ðŸ‘¥ Yangi user: {week_users}\n\n"
        "ðŸŸ£ 1 OY\n"
        f"ðŸ’µ Kirim: {month_income:,.0f} so'm\n"
        f"ðŸ”´ Chiqim: {month_out:,.0f} so'm\n"
        f"ðŸ‘¥ Yangi user: {month_users}\n\n"
        "ðŸ“Œ UMUMIY\n"
        f"ðŸ‘¥ Jami user: {total_users}\n"
        f"ðŸ“¦ Jami buyurtma: {total_orders}\n"
        f"ðŸ’° Userlar balanslari jami: "
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
            "ðŸ‘¥ Foydalanuvchilar yo'q."
        )

        return

    text = "ðŸ‘¥ FOYDALANUVCHILAR\n\n"

    for r in rows:

        text += (
            f"ðŸ‘¤ {r['first_name'] or 'User'}\n"
            f"ðŸ†” ID: {r['user_id']}\n"
            f"ðŸ”— @{r['username'] or 'yoâ€˜q'}\n"
            f"ðŸ’° Balans: "
            f"{r['balance']:,.0f} so'm\n"
            f"ðŸ“… {r['created_at']}\n\n"
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
        "ðŸ‘¤ User ID yuboring.\n\n"
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
            "ðŸ’³ To'lovlar yo'q."
        )

        return

    text = "ðŸ’³ TO'LOVLAR\n\n"

    for r in rows:

        text += (
            f"#{r['id']} | User: {r['user_id']}\n"
            f"ðŸ’° So'ralgan: "
            f"{r['requested_amount']:,.0f}\n"
            f"âœ… Tasdiqlangan: "
            f"{r['approved_amount']:,.0f}\n"
            f"ðŸ“Š {r['status']}\n"
            f"ðŸ• {r['created_at']}\n\n"
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
            "ðŸ“¦ Buyurtmalar yo'q."
        )

        return

    text = "ðŸ“¦ BUYURTMALAR\n\n"

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
            f"ðŸ‘¤ User: {r['user_id']}\n"
            f"ðŸŽ® Game ID: {r['game_id']}\n"
            f"ðŸ“¦ {r['product_name']}\n"
            f"ðŸ†” Player/User ID: {r['player_id']}\n"
        )

        if fields.get("server_id"):

            text += (
                f"ðŸŒ Server ID: "
                f"{fields['server_id']}\n"
            )

        text += (
            f"ðŸ’° Sotuv: "
            f"{r['sale_price']:,.0f} so'm\n"
            f"ðŸ“Š {r['status']}\n"
            f"ðŸ”¢ PlayPay: "
            f"{r['playpay_order_id']}\n"
            f"ðŸ• {r['created_at']}\n\n"
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
            "âŒ Avval ðŸ”„ Katalog tugmasini bosing."
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
        "ðŸ’µ O'zgartiriladigan paketni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            kb
        )
    )


async def price_callback(update, context):

    q = update.callback_query

    if q.from_user.id != ADMIN_ID:

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
            "âŒ ID xato."
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
            "âŒ Mahsulot topilmadi."
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
        f"ðŸ“¦ {r['game_name']}\n"
        f"ðŸŽ {r['package_name']}\n"
        f"ðŸ’° Hozirgi: "
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
        "ðŸŽ Promo yaratish:\n\n"
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
        f"ðŸ’³ Hozirgi karta:\n"
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
        "ðŸ“¢ Kanalga post yuborish.\n\n"
        "Avval post matnini yuboring.\n"
        "Keyin rasm/GIF/video yuboring.\n\n"
        "Faqat matn bo'lsa MATN deb yozing."
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text_handler(update, context):

    if update.effective_user.id != ADMIN_ID:

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
                "âŒ User ID raqam bo'lishi kerak."
            )

            return True

        if not user_exists(uid):

            await update.message.reply_text(
                "âŒ User topilmadi."
            )

            return True

        context.user_data[
            "balance_user"
        ] = uid

        context.user_data[
            "admin_state"
        ] = "balance_amount"

        await update.message.reply_text(
            "âž• Qo'shish: +50000\n"
            "âž– Ayirish: -50000\n\n"
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
                "âŒ Masalan +50000 yoki -50000 yozing."
            )

            return True

        if amount == 0:

            await update.message.reply_text(
                "âŒ 0 mumkin emas."
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
                "âŒ User balansida buncha pul yo'q."
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
                f"ðŸ‘‘ Admin balansingizni o'zgartirdi.\n\n"
                f"{'âž•' if amount > 0 else 'âž–'} "
                f"{abs(amount):,.0f} so'm\n"
                f"ðŸ’° Yangi balans: "
                f"{new_balance:,.0f} so'm"
            )

        except Exception:

            pass

        await update.message.reply_text(
            f"âœ… Bajarildi.\n"
            f"ðŸ‘¤ {uid}\n"
            f"{'âž•' if amount > 0 else 'âž–'} "
            f"{abs(amount):,.0f} so'm\n"
            f"ðŸ’° Yangi balans: "
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
                "âŒ Faqat raqam yozing."
            )

            return True

        if amount <= 0:

            await update.message.reply_text(
                "âŒ Summa 0 dan katta bo'lsin."
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
                "âŒ To'lov topilmadi."
            )

            return True

        if payment["status"] != "pending":

            c.close()

            context.user_data.clear()

            await update.message.reply_text(
                "âš ï¸ Bu to'lov allaqachon ko'rilgan."
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
                f"âœ… To'lov tasdiqlandi!\n\n"
                f"âž• Balansga: "
                f"{amount:,.0f} so'm\n"
                f"ðŸ’° Yangi balans: "
                f"{new_balance:,.0f} so'm"
            )

        except Exception:

            pass

        await update.message.reply_text(
            f"âœ… Balans qo'shildi.\n"
            f"ðŸ‘¤ {payment['user_id']}\n"
            f"âž• {amount:,.0f} so'm",
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
                "âŒ Narx raqam bo'lishi kerak."
            )

            return True

        if price < 0:

            await update.message.reply_text(
                "âŒ Narx 0 yoki undan katta bo'lsin."
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

        await update.message.reply_text(
            f"âœ… Narx o'zgartirildi: "
            f"{price:,.0f} so'm",
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
            f"âœ… Karta saqlandi:\n"
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
                "âŒ Foiz va limit raqam bo'lsin."
            )

            return True

        if (
            percent <= 0
            or percent > 100
            or limit < 0
        ):

            await update.message.reply_text(
                "âŒ Qiymatlar noto'g'ri."
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
            f"âœ… Promo yaratildi!\n"
            f"ðŸŽ {code}\n"
            f"ðŸ’¸ {percent}%\n"
            f"ðŸ”¢ Limit: {limit}",
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
            "âœ… Matn saqlandi.\n\n"
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
                "âœ… Matnli post yuborildi.",
                reply_markup=admin_kb()
            )

        except Exception as e:

            await update.message.reply_text(
                f"âŒ {e}"
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

    if update.effective_user.id != ADMIN_ID:

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
            "âœ… Post kanalga yuborildi.",
            reply_markup=admin_kb()
        )

    except Exception as e:

        await update.message.reply_text(
            f"âŒ Post yuborilmadi: {e}"
        )


# ============================================================
# ORDER STATUS
# ============================================================

async def check_orders(context):

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
                f"ðŸ“¦ Buyurtma #{r['id']}\n\n"
                f"ðŸ“Š Yangi status: {new_status}"
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

    if q.from_user.id != ADMIN_ID:

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
        f"ðŸ† {title} REYTING\n\n"
    )

    n = 1

    for r in rows:

        if r["spent"] <= 0:

            continue

        text += (
            f"{n}. "
            f"{r['first_name'] or 'User'} "
            f"(@{r['username'] or 'yoâ€˜q'})\n"
            f"ðŸ†” {r['user_id']}\n"
            f"ðŸ“¦ Buyurtma: {r['orders']}\n"
            f"ðŸ’° Xarid: "
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

    if q.from_user.id != ADMIN_ID:

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
            "ðŸ† Reytingni tanlang:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "ðŸ† 1 haftalik",
                        callback_data="rating:week"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "ðŸ† 1 oylik",
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
                f"ðŸ” PLAYPAY BALANSI\n\n"
                f"ðŸ’µ USD: "
                f"{b.get('amount', b.get('usd','0'))}\n"
                f"ðŸ’± Valyuta: "
                f"{b.get('currency','USD')}"
            )

        else:

            await q.message.reply_text(
                "âŒ PlayPay balansini olishda xato:\n"
                f"{data.get('error','API xatosi')}"
            )

    # ========================================================
    # FAQAT ADMIN KATALOG YANGILAYDI
    # ========================================================

    elif d == "a_sync":

        await q.message.reply_text(
            "ðŸ”„ PlayPay katalogi yangilanmoqda..."
        )

        ok, result = await asyncio.to_thread(
            sync_catalog
        )

        await q.message.reply_text(
            f"{'âœ…' if ok else 'âŒ'} {result}",
            reply_markup=admin_kb()
        )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):

    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        pass

    d = q.data

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
                f"âŒ Xatolik:\n{e}"
            )

        except Exception:

            pass


# ============================================================
# MEDIA ROUTER
# ============================================================

async def media_router(update, context):

    if update.effective_user.id == ADMIN_ID:

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

    if update.effective_user.id == ADMIN_ID:

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

    context.user_data.clear()

    await update.message.reply_text(
        "âŒ Bekor qilindi.",
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

    init_db()

    ensure_pubg()
    ensure_mobile_legends()

    # --------------------------------------------------------
    # ENV TEKSHIRISH
    # --------------------------------------------------------

    if not BOT_TOKEN:

        raise SystemExit(
            "âŒ BOT_TOKEN Environment Variable yozilmagan."
        )

    if not ADMIN_ID:

        raise SystemExit(
            "âŒ ADMIN_ID Environment Variable yozilmagan."
        )

    if not PLAYPAY_API_KEY:

        raise SystemExit(
            "âŒ PLAYPAY_API_KEY Environment Variable yozilmagan."
        )

    # --------------------------------------------------------
    # TELEGRAM APP
    # --------------------------------------------------------

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Error handler
    app.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel_command
        )
    )

    # --------------------------------------------------------
    # CALLBACK
    # --------------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # --------------------------------------------------------
    # MEDIA
    # --------------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.PHOTO
            |
            filters.ANIMATION
            |
            filters.VIDEO,
            media_router
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT
            &
            ~filters.COMMAND,
            text_router
        )
    )

    # --------------------------------------------------------
    # ORDER STATUS
    # --------------------------------------------------------

    if app.job_queue:

        app.job_queue.run_repeating(
            check_orders,
            interval=30,
            first=30
        )

    # ========================================================
    # MUHIM:
    # startup_catalog OLIB TASHLANDI.
    #
    # Bot ishga tushganda katalog API'dan yangilanmaydi.
    #
    # Katalog faqat:
    # ADMIN -> ðŸ”„ Katalog
    # orqali sync qilinadi.
    # ========================================================

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

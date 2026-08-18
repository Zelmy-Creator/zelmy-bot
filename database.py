
#SQLite вместо JSON-файлов.
 #Почему это важно: JSON-файлы читались/писались целиком на каждое изменение — 
#при двух одновременных записях (два юзера пишут боту в одну секунду) можно было 
#потерять данные (последняя запись просто перезатирала файл без учёта параллельной). 
#SQLite с блокировкой на запись решает это, плюс он ощутимо быстрее на больших 
#объёмах (не нужно перечитывать/переписывать весь файл ради одной строки).
#При первом запуске (если zelmy.db ещё нет, а старые *.json есть) выполняется 
#одноразовая миграция — старые данные не теряются.
"""
import sqlite3
import json
import os
import time
import logging
import threading
from datetime import datetime, timedelta
import config

_lock = threading.Lock()  # sqlite3 в threaded-режиме требует сериализации записи
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

def _executescript(sql):
    with _lock:
        _conn.executescript(sql)
        _conn.commit()

def init_db():
    _executescript("""
CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            first_seen TEXT,
            banned INTEGER DEFAULT 0,
            referred_by INTEGER,
            referral_count INTEGER DEFAULT 0,
            persona TEXT DEFAULT 'default'
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            plan TEXT,
            expires_at REAL
        );
        CREATE TABLE IF NOT EXISTS usage_daily (
            user_id INTEGER PRIMARY KEY,
            date TEXT,
            count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT,
            ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_history_chat ON chat_history(chat_id, ts);
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            plan TEXT,
            days INTEGER,
            max_uses INTEGER,
            created_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS promo_uses (
            code TEXT,
            user_id INTEGER,
            PRIMARY KEY (code, user_id)
        );
        CREATE TABLE IF NOT EXISTS stats_events (
            name TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stats_daily_active (
            date TEXT,
            user_id INTEGER,
            PRIMARY KEY (date, user_id)
        );
        CREATE TABLE IF NOT EXISTS stats_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            context TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS campaign (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            plan TEXT,
            days INTEGER,
            slots_left INTEGER
        );
        CREATE TABLE IF NOT EXISTS campaign_granted (
            user_id INTEGER PRIMARY KEY
        );
    """)
    _migrate_from_json_if_needed()

def _migrate_from_json_if_needed():
    """Одноразовая миграция: если рядом лежат старые *.json - переносим их в SQLite,
    затем переименовываем в *.json.migrated, чтобы не мигрировать повторно."""
    def load(fname):
        if not os.path.exists(fname):
            return None
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Миграция: не удалось прочитать {fname}: {e}")
            return None

    users = load("users.json")
    if users:
        for uid, info in users.items():
            with _lock:
                _conn.execute("""
                    INSERT OR IGNORE INTO users (id, username, first_name, first_seen, banned, referred_by, referral_count, persona)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (int(uid), info.get("username"), info.get("first_name"), info.get("first_seen"),
                      int(bool(info.get("banned", False))), info.get("referred_by"),
                      info.get("referral_count", 0), info.get("persona", "default")))
                _conn.commit()
        logging.info(f"Миграция: перенесено {len(users)} пользователей из users.json")
        os.rename("users.json", "users.json.migrated")

    subs = load("subscriptions.json")
    if subs:
        for uid, info in subs.items():
            with _lock:
                _conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, expires_at) VALUES (?, ?, ?)",
                             (int(uid), info.get("plan"), info.get("expires_at", 0)))
                _conn.commit()
        logging.info(f"Миграция: перенесено {len(subs)} подписок из subscriptions.json")
        os.rename("subscriptions.json", "subscriptions.json.migrated")

    usage = load("usage.json")
    if usage:
        for uid, info in usage.items():
            with _lock:
                _conn.execute("INSERT OR REPLACE INTO usage_daily (user_id, date, count) VALUES (?, ?, ?)",
                             (int(uid), info.get("date"), info.get("count", 0)))
                _conn.commit()
        os.rename("usage.json", "usage.json.migrated")

    history = load("chat_history.json")
    if history:
        now = time.time()
        for chat_id, messages in history.items():
            for msg in messages:
                with _lock:
                    _conn.execute("INSERT INTO chat_history (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                                 (int(chat_id), msg.get("role"), msg.get("content"), now))
            _conn.commit()
        logging.info(f"Миграция: перенесена история {len(history)} чатов из chat_history.json")
        os.rename("chat_history.json", "chat_history.json.migrated")

    promo = load("promocodes.json")
    if promo:
        for code, info in promo.items():
            with _lock:
                _conn.execute("INSERT OR REPLACE INTO promocodes (code, plan, days, max_uses, created_by) VALUES (?, ?, ?, ?, ?)",
                             (code, info.get("plan"), info.get("days"), info.get("max_uses"), info.get("created_by")))
                _conn.commit()
        os.rename("promocodes.json", "promocodes.json.migrated")

# ---------- ПОЛЬЗОВАТЕЛИ ----------
def register_user(user_id, username=None, first_name=None):
    with _lock:
        row = _conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        is_new = row is None
        if is_new:
            _conn.execute("""
                INSERT INTO users (id, username, first_name, first_seen, banned, referral_count, persona)
                VALUES (?, ?, ?, ?, 0, 0, 'default')
            """, (user_id, username, first_name, datetime.now().strftime("%Y-%m-%d %H:%M")))
        else:
            _conn.execute("UPDATE users SET username=?, first_name=? WHERE id=?",
                         (username, first_name, user_id))
        _conn.commit()
        return is_new

def get_user(user_id):
    row = _conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None

def get_all_users():
    return [dict(r) for r in _conn.execute("SELECT * FROM users").fetchall()]

def is_banned(user_id):
    row = _conn.execute("SELECT banned FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row["banned"]) if row else False

def set_banned(user_id, banned: bool):
    with _lock:
        _conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), user_id))
        _conn.commit()

def set_persona(user_id, persona):
    with _lock:
        _conn.execute("UPDATE users SET persona=? WHERE id=?", (persona, user_id))
        _conn.commit()

def get_persona(user_id):
    row = _conn.execute("SELECT persona FROM users WHERE id=?", (user_id,)).fetchone()
    return row["persona"] if row and row["persona"] else "default"

def set_referred_by(user_id, referrer_id):
    with _lock:
        _conn.execute("UPDATE users SET referred_by=? WHERE id=?", (referrer_id, user_id))
        _conn.commit()

def increment_referral_count(referrer_id):
    with _lock:
        _conn.execute("UPDATE users SET referral_count = referral_count + 1 WHERE id=?", (referrer_id,))
        _conn.commit()
    row = _conn.execute("SELECT referral_count FROM users WHERE id=?", (referrer_id,)).fetchone()
    return row["referral_count"] if row else 0

# ---------- ПОДПИСКИ ----------
def get_subscription(user_id):
    row = _conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None

def set_subscription(user_id, plan, expires_at):
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, expires_at) VALUES (?, ?, ?)",
                     (user_id, plan, expires_at))
        _conn.commit()

def extend_subscription(user_id, plan, extra_days):
    """Продлевает подписку от текущей даты истечения (или от сейчас, если её не было/истекла)."""
    current = get_subscription(user_id)
    base_point = max(current["expires_at"], time.time()) if current else time.time()
    new_expiry = base_point + extra_days * 24 * 60 * 60
    set_subscription(user_id, plan, new_expiry)
    return new_expiry

def count_active_subscriptions():
    row = _conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE expires_at > ?",
                        (time.time(),)).fetchone()
    return row["c"]

# ---------- ДНЕВНОЙ ЛИМИТ (free) ----------
def get_usage_count_today(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    row = _conn.execute("SELECT date, count FROM usage_daily WHERE user_id=?", (user_id,)).fetchone()
    if not row or row["date"] != today:
        return 0
    return row["count"]

def increment_usage_today(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    current = get_usage_count_today(user_id)
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO usage_daily (user_id, date, count) VALUES (?, ?, ?)",
                     (user_id, today, current + 1))
        _conn.commit()
    return current + 1

# ---------- ИСТОРИЯ ДИАЛОГА ----------
def add_history_message(chat_id, role, content):
    with _lock:
        _conn.execute("INSERT INTO chat_history (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                     (chat_id, role, content, time.time()))
        _conn.commit()

def get_history(chat_id, limit=100):
    rows = _conn.execute("""
        SELECT role, content FROM chat_history
        WHERE chat_id=?
        ORDER BY ts DESC LIMIT ?
    """, (chat_id, limit)).fetchall()
    return [dict(r) for r in rows][::-1]

def prune_old_history():
    cutoff = time.time() - config.HISTORY_RETENTION_DAYS * 24 * 60 * 60
    with _lock:
        cur = _conn.execute("DELETE FROM chat_history WHERE ts < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount

# ---------- ПРОМОКОДЫ ----------
def create_promocode(code, plan, days, max_uses, created_by):
    with _lock:
        _conn.execute("INSERT INTO promocodes (code, plan, days, max_uses, created_by) VALUES (?, ?, ?, ?, ?)",
                     (code, plan, days, max_uses, created_by))
        _conn.commit()

def get_promocode(code):
    row = _conn.execute("SELECT * FROM promocodes WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None

def promocode_use_count(code):
    row = _conn.execute("SELECT COUNT(*) c FROM promo_uses WHERE code=?", (code,)).fetchone()
    return row["c"]

def has_used_promocode(code, user_id):
    row = _conn.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, user_id)).fetchone()
    return row is not None

def redeem_promocode(code, user_id):
    with _lock:
        _conn.execute("INSERT INTO promo_uses (code, user_id) VALUES (?, ?)", (code, user_id))
        _conn.commit()

# ---------- СТАТИСТИКА ----------
def track_event(event_name):
    with _lock:
        _conn.execute("""
            INSERT INTO stats_events (name, count) VALUES (?, 1)
            ON CONFLICT(name) DO UPDATE SET count = count + 1
        """, (event_name,))
        _conn.commit()

def track_daily_active(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO stats_daily_active (date, user_id) VALUES (?, ?)",
                     (today, user_id))
        _conn.commit()

def get_active_today_count():
    today = datetime.now().strftime("%Y-%m-%d")
    row = _conn.execute("SELECT COUNT(*) c FROM stats_daily_active WHERE date=?", (today,)).fetchone()
    return row["c"]

def get_all_events():
    rows = _conn.execute("SELECT name, count FROM stats_events ORDER BY count DESC").fetchall()
    return [(r["name"], r["count"]) for r in rows]

def track_error(context, error_text):
    with _lock:
        _conn.execute("INSERT INTO stats_errors (time, context, error) VALUES (?, ?, ?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), context, str(error_text)[:300]))
        # держим только последние 200 записей, чтобы таблица не росла бесконечно
        _conn.execute("""
            DELETE FROM stats_errors 
            WHERE id NOT IN (
                SELECT id FROM stats_errors ORDER BY id DESC LIMIT 200
            )
        """)
        _conn.commit()

def get_error_count():
    row = _conn.execute("SELECT COUNT(*) c FROM stats_errors").fetchone()
    return row["c"]

def get_last_errors(n=5):
    rows = _conn.execute("SELECT time, context, error FROM stats_errors ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]

# ---------- ПРОМО-АКЦИИ («первым N — подарок») ----------
def start_campaign(plan, days, slots):
    with _lock:
        _conn.execute("DELETE FROM campaign")
        _conn.execute("DELETE FROM campaign_granted")
        _conn.execute("INSERT INTO campaign (id, plan, days, slots_left) VALUES (1, ?, ?, ?)",
                     (plan, days, slots))
        _conn.commit()

def stop_campaign():
    with _lock:
        _conn.execute("DELETE FROM campaign")
        _conn.commit()

def get_active_campaign():
    row = _conn.execute("SELECT * FROM campaign WHERE id=1").fetchone()
    return dict(row) if row else None

def grant_campaign_reward_if_eligible(user_id):
    campaign = get_active_campaign()
    if not campaign or campaign["slots_left"] <= 0:
        return None
    already = _conn.execute("SELECT 1 FROM campaign_granted WHERE user_id=?", (user_id,)).fetchone()
    if already:
        return None
    plan, days = campaign["plan"], campaign["days"]
    extend_subscription(user_id, plan, days)
    with _lock:
        _conn.execute("INSERT INTO campaign_granted (user_id) VALUES (?)", (user_id,))
        _conn.execute("UPDATE campaign SET slots_left = slots_left - 1 WHERE id=1")
        _conn.commit()
    remaining = campaign["slots_left"] - 1
    if remaining <= 0:
        stop_campaign()
    return plan, days

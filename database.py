import sqlite3
import json
import os
import time
import logging
import threading
from datetime import datetime
import config

_lock = threading.Lock()
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
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT,
            ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_history_chat ON chat_history(chat_id);
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
    """)
    _migrate_from_json_if_needed()

def _migrate_from_json_if_needed():
    """Переносим только то, что ещё актуально: пользователей и историю переписки."""
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
                """, (
                    int(uid),
                    info.get("username"),
                    info.get("first_name"),
                    info.get("first_seen"),
                    int(bool(info.get("banned", False))),
                    info.get("referred_by"),
                    info.get("referral_count", 0),
                    info.get("persona", "default")
                ))
                _conn.commit()
        logging.info(f"Миграция: перенесено {len(users)} пользователей из users.json")
        os.rename("users.json", "users.json.migrated")

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
# ---------- ПОЛЬЗОВАТЕЛИ ----------
def track_user(user_id, username, first_name):
    with _lock:
        row = _conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        is_new = row is None
        if is_new:
            _conn.execute(
                "INSERT INTO users (id, username, first_name, first_seen, banned, referral_count, persona) VALUES (?, ?, ?, ?, 0, 0, 'default')",
                (user_id, username, first_name, datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
        else:
            _conn.execute("UPDATE users SET username=?, first_name=? WHERE id=?", (username, first_name, user_id))
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

# --- ИСТОРИЯ ДИАЛОГА (долгая память, 30 дней) ---
def add_history_message(chat_id, role, content):
    with _lock:
        _conn.execute("INSERT INTO chat_history (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                      (chat_id, role, content, time.time()))
        _conn.commit()

def get_recent_history(chat_id, limit):
    cutoff = time.time() - config.HISTORY_RETENTION_DAYS * 86400
    rows = _conn.execute(
        "SELECT role, content FROM chat_history WHERE chat_id=? AND ts >= ? ORDER BY ts DESC LIMIT ?",
        (chat_id, cutoff, limit)
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def clear_history(chat_id):
    with _lock:
        _conn.execute("DELETE FROM chat_history WHERE chat_id=?", (chat_id,))
        _conn.commit()

def prune_old_history():
    cutoff = time.time() - config.HISTORY_RETENTION_DAYS * 86400
    with _lock:
        cur = _conn.execute("DELETE FROM chat_history WHERE ts < ?", (cutoff,))
        _conn.commit()
    return cur.rowcount
    # ---------- СТАТИСТИКА ----------
def track_event(event_name):
    with _lock:
        _conn.execute("INSERT INTO stats_events (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1", (event_name,))
        _conn.commit()

def track_daily_active(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO stats_daily_active (date, user_id) VALUES (?, ?)", (today, user_id))
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
        _conn.execute("""
            DELETE FROM stats_errors WHERE id NOT IN (
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

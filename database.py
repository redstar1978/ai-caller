import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'ai_caller.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_number TEXT,
    caller_name TEXT,
    start_time TEXT,
    end_time TEXT,
    duration INTEGER DEFAULT 0,
    status TEXT DEFAULT 'answered',
    recording_path TEXT,
    transcript TEXT,
    ai_summary TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER REFERENCES calls(id),
    direction TEXT,
    content TEXT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER REFERENCES calls(id),
    subject TEXT,
    body TEXT,
    is_read INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reply_text TEXT DEFAULT '',
    reply_channel TEXT DEFAULT '',
    callback_status TEXT DEFAULT '',
    callback_result TEXT DEFAULT '',
    telegram_msg_id INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telegram_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    bot_token TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_MIGRATIONS = [
    "ALTER TABLE inbox ADD COLUMN reply_text TEXT DEFAULT ''",
    "ALTER TABLE inbox ADD COLUMN reply_channel TEXT DEFAULT ''",
    "ALTER TABLE inbox ADD COLUMN callback_status TEXT DEFAULT ''",
    "ALTER TABLE inbox ADD COLUMN callback_result TEXT DEFAULT ''",
    "ALTER TABLE inbox ADD COLUMN telegram_msg_id INTEGER DEFAULT NULL",
]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Run migrations for existing DBs (ignore errors if column already exists)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except Exception:
                pass


# ── Config ────────────────────────────────────────────────────────────────────

def get_config(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value) if value is not None else None, datetime.utcnow().isoformat())
        )


def get_all_config():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}


# ── Calls ─────────────────────────────────────────────────────────────────────

def create_call(caller_number, caller_name=""):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO calls(caller_number, caller_name, start_time) VALUES(?,?,?)",
            (caller_number, caller_name, datetime.utcnow().isoformat())
        )
        return cur.lastrowid


def update_call(call_id, **kwargs):
    allowed = {"end_time", "duration", "status", "recording_path", "transcript", "ai_summary", "caller_name"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [call_id]
    with get_db() as conn:
        conn.execute(f"UPDATE calls SET {set_clause} WHERE id=?", values)


def get_call(call_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()


def list_calls(limit=50, offset=0):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def count_calls():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]


# ── Messages ──────────────────────────────────────────────────────────────────

def add_message(call_id, direction, content):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages(call_id, direction, content) VALUES(?,?,?)",
            (call_id, direction, content)
        )


def get_messages(call_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE call_id=? ORDER BY timestamp",
            (call_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Inbox ─────────────────────────────────────────────────────────────────────

def create_inbox_entry(call_id, subject, body):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO inbox(call_id, subject, body) VALUES(?,?,?)",
            (call_id, subject, body)
        )
        return cur.lastrowid


def list_inbox(unread_only=False, limit=50, offset=0):
    with get_db() as conn:
        where = "WHERE i.is_read=0" if unread_only else ""
        rows = conn.execute(
            f"""SELECT i.*, c.caller_number, c.caller_name, c.duration
                FROM inbox i LEFT JOIN calls c ON i.call_id=c.id
                {where}
                ORDER BY i.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def count_unread():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM inbox WHERE is_read=0").fetchone()[0]


def mark_read(inbox_id):
    with get_db() as conn:
        conn.execute("UPDATE inbox SET is_read=1 WHERE id=?", (inbox_id,))


def mark_all_read():
    with get_db() as conn:
        conn.execute("UPDATE inbox SET is_read=1")


def delete_inbox_entry(inbox_id):
    with get_db() as conn:
        conn.execute("DELETE FROM inbox WHERE id=?", (inbox_id,))


def get_inbox_entry(inbox_id):
    with get_db() as conn:
        row = conn.execute(
            """SELECT i.*, c.caller_number, c.caller_name
               FROM inbox i LEFT JOIN calls c ON i.call_id=c.id
               WHERE i.id=?""",
            (inbox_id,)
        ).fetchone()
        return dict(row) if row else None


def save_reply(inbox_id, reply_text, reply_channel):
    with get_db() as conn:
        conn.execute(
            "UPDATE inbox SET reply_text=?, reply_channel=?, callback_status='pending' WHERE id=?",
            (reply_text, reply_channel, inbox_id)
        )


def set_callback_status(inbox_id, status, result=None):
    with get_db() as conn:
        if result is not None:
            conn.execute(
                "UPDATE inbox SET callback_status=?, callback_result=? WHERE id=?",
                (status, result, inbox_id)
            )
        else:
            conn.execute(
                "UPDATE inbox SET callback_status=? WHERE id=?",
                (status, inbox_id)
            )


def set_telegram_msg_id(inbox_id, msg_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE inbox SET telegram_msg_id=? WHERE id=?",
            (msg_id, inbox_id)
        )


def get_inbox_by_telegram_msg_id(msg_id):
    with get_db() as conn:
        row = conn.execute(
            """SELECT i.*, c.caller_number, c.caller_name
               FROM inbox i LEFT JOIN calls c ON i.call_id=c.id
               WHERE i.telegram_msg_id=?""",
            (msg_id,)
        ).fetchone()
        return dict(row) if row else None


def get_pending_callbacks():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT i.*, c.caller_number, c.caller_name
               FROM inbox i LEFT JOIN calls c ON i.call_id=c.id
               WHERE i.callback_status='pending'
               ORDER BY i.created_at""",
        ).fetchall()
        return [dict(r) for r in rows]


# ── Users ─────────────────────────────────────────────────────────────────────

def count_users():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username, password_hash, role="user"):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, role) VALUES(?,?,?)",
            (username, password_hash, role)
        )
        return cur.lastrowid


def get_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def list_users():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def update_user(user_id, **kwargs):
    allowed = {"username", "password_hash", "role", "is_active"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id]
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {set_clause} WHERE id=?", values)


def delete_user(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


# ── Auth sessions (remember-me) ───────────────────────────────────────────────

def create_auth_session(user_id, token, expires_at):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO auth_sessions(user_id, token, expires_at) VALUES(?,?,?)",
            (user_id, token, expires_at)
        )


def get_user_by_token(token):
    with get_db() as conn:
        row = conn.execute(
            """SELECT u.* FROM users u
               JOIN auth_sessions s ON s.user_id = u.id
               WHERE s.token=? AND s.expires_at > datetime('now') AND u.is_active=1""",
            (token,)
        ).fetchone()
        return dict(row) if row else None


def delete_auth_session(token):
    with get_db() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))


def delete_user_sessions(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))


# ── Auth codes (2FA) ──────────────────────────────────────────────────────────

def create_auth_code(user_id, code, expires_at):
    # Invalidate old unused codes for this user first
    with get_db() as conn:
        conn.execute("DELETE FROM auth_codes WHERE user_id=? AND used=0", (user_id,))
        conn.execute(
            "INSERT INTO auth_codes(user_id, code, expires_at) VALUES(?,?,?)",
            (user_id, code, expires_at)
        )


def get_valid_auth_code(user_id, code):
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM auth_codes
               WHERE user_id=? AND code=? AND used=0
               AND expires_at > datetime('now')""",
            (user_id, code)
        ).fetchone()
        return dict(row) if row else None


def mark_auth_code_used(code_id):
    with get_db() as conn:
        conn.execute("UPDATE auth_codes SET used=1 WHERE id=?", (code_id,))


# ── Telegram recipients ───────────────────────────────────────────────────────

def list_telegram_recipients() -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM telegram_recipients ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def create_telegram_recipient(name: str, bot_token: str, chat_id: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO telegram_recipients (name, bot_token, chat_id) VALUES (?,?,?)",
            (name, bot_token, chat_id)
        )
        return cur.lastrowid


def delete_telegram_recipient(recipient_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM telegram_recipients WHERE id=?", (recipient_id,))


def toggle_telegram_recipient(recipient_id: int, enabled: bool):
    with get_db() as conn:
        conn.execute(
            "UPDATE telegram_recipients SET enabled=? WHERE id=?",
            (1 if enabled else 0, recipient_id)
        )

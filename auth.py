"""
Authentication helpers for AI-Caller.

Provides:
  - login_required / admin_required decorators
  - Password hashing (SHA-256 + salt via secrets)
  - Session management (Flask session + remember-me cookie)
  - 2FA code generation and delivery (Telegram or e-mail)
"""

import functools
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from flask import g, redirect, request, session, url_for

import database as db

# Cookie name for remember-me token
_COOKIE = "ai_caller_remember"
# 2FA code validity
_CODE_TTL_MINUTES = 10
# Remember-me validity
_REMEMBER_DAYS = 90


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a salted SHA-256 hex digest."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored: str) -> bool:
    """Verify password against stored 'salt:hash' string."""
    try:
        salt, h = stored.split(":", 1)
        expected = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        return hmac.compare_digest(expected, h)
    except Exception:
        return False


# ── Session / current user ────────────────────────────────────────────────────

def get_current_user():
    """Return the logged-in user dict or None."""
    # 1. remember-me cookie
    token = request.cookies.get(_COOKIE)
    if token:
        user = db.get_user_by_token(token)
        if user and user.get("is_active"):
            session["user_id"] = user["id"]
            return user
    # 2. Flask session
    uid = session.get("user_id")
    if uid:
        user = db.get_user(uid)
        if user and user.get("is_active"):
            return user
    return None


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            return redirect(url_for("index"))
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


# ── Remember-me token ─────────────────────────────────────────────────────────

def create_remember_token(user_id: int) -> str:
    token = secrets.token_urlsafe(40)
    expires = (datetime.utcnow() + timedelta(days=_REMEMBER_DAYS)).isoformat()
    db.create_auth_session(user_id, token, expires)
    return token


# ── 2FA ───────────────────────────────────────────────────────────────────────

def generate_2fa_code() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


def send_2fa_code(user_id: int, cfg: dict) -> tuple[bool, str]:
    """Generate a 2FA code, store it, and send via configured channel.
    Returns (ok, error_message).
    """
    code = generate_2fa_code()
    expires = (datetime.utcnow() + timedelta(minutes=_CODE_TTL_MINUTES)).isoformat()
    db.create_auth_code(user_id, code, expires)

    text = (
        f"Ihr AI-Caller Login-Code: *{code}*\n"
        f"(gültig {_CODE_TTL_MINUTES} Minuten)"
    )
    channel = cfg.get("two_factor_channel", "telegram")

    if channel == "email":
        from notifications import send_email
        smtp_host = cfg.get("email_smtp_host", "")
        smtp_port = int(cfg.get("email_smtp_port", "465") or "465")
        smtp_user = cfg.get("email_smtp_user", "")
        smtp_pass = cfg.get("email_smtp_pass", "")
        from_addr = cfg.get("email_from", smtp_user)
        to_addr = cfg.get("email_to", "")
        use_ssl = cfg.get("email_use_ssl", "true").lower() == "true"
        if not smtp_host or not to_addr:
            return False, "E-Mail nicht konfiguriert (SMTP + Empfänger unter Benachrichtigungen prüfen)"
        plain = text.replace("*", "")
        ok, msg = send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
                             to_addr, "AI-Caller Login-Code", plain, use_ssl=use_ssl)
        return ok, msg
    else:  # telegram
        from notifications import send_telegram
        token = cfg.get("telegram_bot_token", "")
        chat_id = cfg.get("telegram_chat_id", "")
        if not token or not chat_id:
            return False, "Telegram nicht konfiguriert (Token + Chat-ID unter Benachrichtigungen prüfen)"
        ok, msg, _ = send_telegram(token, chat_id, text)
        return ok, msg


def verify_2fa_code(user_id: int, code: str) -> bool:
    """Returns True if the code is valid, not expired, and not already used."""
    entry = db.get_valid_auth_code(user_id, code)
    if not entry:
        return False
    db.mark_auth_code_used(entry["id"])
    return True

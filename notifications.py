"""
Notification system – Telegram bot and/or Email.
"""

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_telegram(token: str, chat_id: str, text: str,
                  reply_markup: dict = None) -> tuple[bool, str, int | None]:
    """Send a message via Telegram Bot API.

    Returns (ok, message_or_error, telegram_message_id).
    """
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=10)
        # Telegram always returns HTTP 200 — real errors are in the JSON body
        data = resp.json()
        if not data.get("ok"):
            desc = data.get("description", "Unbekannter Telegram-Fehler")
            logger.error(f"Telegram error: {desc}")
            return False, desc, None
        msg_id = data.get("result", {}).get("message_id")
        return True, "OK", msg_id
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False, str(e), None


def get_telegram_chat_id(token: str) -> tuple[bool, str, str]:
    """Fetch the most recent chat_id from the bot's getUpdates.

    Returns (ok, chat_id, chat_name_or_error).
    The user must have sent the bot at least one message first.
    """
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=10&timeout=0"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            return False, "", data.get("description", "Ungültiger Bot-Token")
        updates = data.get("result", [])
        if not updates:
            return False, "", (
                "Keine Nachrichten gefunden. Schicke deinem Bot zuerst "
                "eine Nachricht (z.B. /start) und versuche es erneut."
            )
        for update in reversed(updates):
            msg = update.get("message") or update.get("channel_post")
            if msg and "chat" in msg:
                chat = msg["chat"]
                chat_id = str(chat["id"])
                name = (
                    (chat.get("first_name", "") + " " + chat.get("last_name", "")).strip()
                    or chat.get("title", "")
                    or chat.get("username", "")
                )
                return True, chat_id, name
        return False, "", "Chat-ID konnte nicht ermittelt werden."
    except Exception as e:
        return False, "", str(e)


def send_email(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str,
               from_addr: str, to_addr: str, subject: str, body: str,
               use_tls: bool = True) -> tuple[bool, str]:
    """Send an email via SMTP."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.attach(MIMEText(body, "plain", "utf-8"))

        context = ssl.create_default_context()
        if use_tls:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(from_addr, to_addr, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(smtp_user, smtp_pass)
                server.sendmail(from_addr, to_addr, msg.as_string())
        return True, "OK"
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False, str(e)


def send_notifications(subject: str, body: str, caller: str, cfg: dict,
                       inbox_id: int = None):
    """Send all enabled notifications based on config.

    If inbox_id is given, stores the Telegram message_id so replies can be matched.
    """
    # Telegram
    if cfg.get("telegram_enabled", "false").lower() == "true":
        token = cfg.get("telegram_bot_token", "")
        chat_id = cfg.get("telegram_chat_id", "")
        if token and chat_id:
            # Extract summary from body (format: "**Zusammenfassung:** ...\n\n**Transkript:**\n...")
            summary = body.split("\n\n**Transkript:**")[0].replace("**Zusammenfassung:** ", "").strip()
            text = f"📞 *Neuer Anruf von {caller}*\n\n*Zusammenfassung:*\n{summary}"
            markup = None
            if inbox_id:
                markup = {"inline_keyboard": [[{
                    "text": "Zur Nachricht",
                    "url": f"http://localhost:5000/inbox/{inbox_id}"
                }]]}
            ok, msg, tg_msg_id = send_telegram(token, chat_id, text, reply_markup=markup)
            if ok and tg_msg_id and inbox_id:
                try:
                    import database as db
                    db.set_telegram_msg_id(inbox_id, tg_msg_id)
                except Exception as e:
                    logger.warning(f"Could not store telegram_msg_id: {e}")
            if not ok:
                logger.warning(f"Telegram notification failed: {msg}")

            # Additional recipients
            try:
                import database as _db
                for r in _db.list_telegram_recipients():
                    if not r.get("enabled"):
                        continue
                    r_token = r.get("bot_token", "")
                    r_chat = r.get("chat_id", "")
                    if not r_token or not r_chat:
                        continue
                    ok2, msg2, _ = send_telegram(r_token, r_chat, text, reply_markup=markup)
                    if not ok2:
                        logger.warning(f"Telegram recipient '{r['name']}' failed: {msg2}")
            except Exception as e:
                logger.warning(f"Additional Telegram recipients error: {e}")
        else:
            logger.warning("Telegram enabled but token/chat_id missing")

    # Email
    if cfg.get("email_enabled", "false").lower() == "true":
        smtp_host = cfg.get("email_smtp_host", "")
        smtp_port = int(cfg.get("email_smtp_port", 465))
        smtp_user = cfg.get("email_smtp_user", "")
        smtp_pass = cfg.get("email_smtp_pass", "")
        from_addr = cfg.get("email_from", smtp_user)
        to_addr = cfg.get("email_to", "")
        use_tls = cfg.get("email_use_ssl", "true").lower() == "true"

        if all([smtp_host, smtp_user, smtp_pass, to_addr]):
            ok, msg = send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
                                 from_addr, to_addr, subject, body, use_tls)
            if not ok:
                logger.warning(f"Email notification failed: {msg}")
        else:
            logger.warning("Email enabled but SMTP settings incomplete")


def test_telegram(token: str, chat_id: str) -> tuple[bool, str]:
    ok, msg, _ = send_telegram(token, chat_id, "✅ AI-Caller Telegram Test erfolgreich!")
    return ok, msg


def test_email(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str,
               from_addr: str, to_addr: str, use_tls: bool = True) -> tuple[bool, str]:
    return send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
                      from_addr, to_addr, "AI-Caller Test", "Testmail erfolgreich.", use_tls)


def poll_telegram_replies(token: str, offset: int = 0) -> tuple[list, int]:
    """Poll Telegram getUpdates for reply messages to bot notifications.

    Returns (replies, next_offset) where replies is a list of
    {"inbox_id": int, "reply_text": str, "tg_update_id": int}.
    Only messages that are replies (reply_to_message) to a bot message are returned.
    """
    try:
        import requests
        import database as db
        url = (f"https://api.telegram.org/bot{token}/getUpdates"
               f"?offset={offset}&limit=50&timeout=0")
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            return [], offset
        updates = data.get("result", [])
        replies = []
        next_offset = offset
        for upd in updates:
            next_offset = max(next_offset, upd["update_id"] + 1)
            msg = upd.get("message")
            if not msg:
                continue
            reply_to = msg.get("reply_to_message")
            if not reply_to:
                continue
            reply_msg_id = reply_to.get("message_id")
            reply_text = msg.get("text", "").strip()
            if not reply_text or not reply_msg_id:
                continue
            # Look up which inbox entry this Telegram message belongs to
            entry = db.get_inbox_by_telegram_msg_id(reply_msg_id)
            if entry and entry.get("callback_status") in ("", None):
                replies.append({
                    "inbox_id": entry["id"],
                    "reply_text": reply_text,
                    "tg_update_id": upd["update_id"],
                })
        return replies, next_offset
    except Exception as e:
        logger.error(f"Telegram reply poll error: {e}")
        return [], offset


def send_callback_result_notification(inbox_id: int, caller_response: str, cfg: dict):
    """Send the caller's callback response back via all enabled notification channels."""
    try:
        import database as db
        entry = db.get_inbox_entry(inbox_id)
        if not entry:
            return
        subject = entry.get("subject", "Rückruf")
        caller = entry.get("caller_number", "Unbekannt")
        text = (f"📞 *Rückruf-Antwort von {caller}*\n"
                f"Bezug: _{subject}_\n\n"
                f"{caller_response}")
        if cfg.get("telegram_enabled", "false").lower() == "true":
            token = cfg.get("telegram_bot_token", "")
            chat_id = cfg.get("telegram_chat_id", "")
            if token and chat_id:
                ok, msg, _ = send_telegram(token, chat_id, text)
                if not ok:
                    logger.warning(f"Callback result Telegram failed: {msg}")
        if cfg.get("email_enabled", "false").lower() == "true":
            smtp_host = cfg.get("email_smtp_host", "")
            smtp_port = int(cfg.get("email_smtp_port", 465) or 465)
            smtp_user = cfg.get("email_smtp_user", "")
            smtp_pass = cfg.get("email_smtp_pass", "")
            from_addr = cfg.get("email_from", smtp_user) or smtp_user
            to_addr = cfg.get("email_to", "")
            use_tls = cfg.get("email_use_ssl", "true").lower() == "true"
            if all([smtp_host, smtp_user, smtp_pass, to_addr]):
                plain = f"Rückruf-Antwort von {caller}\nBezug: {subject}\n\n{caller_response}"
                send_email(smtp_host, smtp_port, smtp_user, smtp_pass,
                           from_addr, to_addr,
                           f"Rückruf-Antwort: {subject}", plain, use_tls)
    except Exception as e:
        logger.error(f"send_callback_result_notification error: {e}")

import logging
import os
import threading
import time
import requests
from datetime import datetime

from flask import Flask, Response, g, jsonify, make_response, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO

import ai_engine
import auth
import database as db
import sip_handler
from notifications import (test_email, test_telegram, get_telegram_chat_id,
                            send_telegram, poll_telegram_replies)

APP_VERSION = "1.2.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Persistent secret key so sessions survive service restarts
_KEY_FILE = os.path.join(os.path.dirname(__file__), "data", ".secret_key")
os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
if os.path.exists(_KEY_FILE):
    with open(_KEY_FILE, "rb") as _f:
        app.secret_key = _f.read()
else:
    import secrets as _sec
    _sk = _sec.token_bytes(32)
    with open(_KEY_FILE, "wb") as _f:
        _f.write(_sk)
    app.secret_key = _sk

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")
sip_handler.set_socketio(socketio)

# ── Init ──────────────────────────────────────────────────────────────────────

db.init_db()

DEFAULTS = {
    "greeting_text": "Hallo, Sie haben den KI-Anrufbeantworter erreicht. Bitte hinterlassen Sie Ihre Nachricht nach dem Signalton.",
    "behavior_prompt": (
        "Du bist ein freundlicher KI-Anrufbeantworter. Deine Aufgabe ist es, "
        "die Nachricht des Anrufers entgegenzunehmen und bei Bedarf einfache Fragen zu beantworten. "
        "Sei höflich, präzise und freundlich. Wenn der Anrufer fertig ist, bedanke dich und verabschiede dich."
    ),
    "max_conversation_turns": "3",
    "answer_delay_seconds": "2",
    "save_to_inbox": "true",
    "save_recordings": "true",
    # VAD / silence detection & hold music
    "silence_timeout_seconds": "3.0",
    "silence_threshold": "2",
    "hold_music_enabled": "true",
    # Callback
    "callback_greeting": "",
    # Schedule
    "schedule_mode": "always",
    "schedule_start": "08:00",
    "schedule_end": "20:00",
    "schedule_days": "0,1,2,3,4,5,6",
    # Phone
    "phone_mode": "",
    "sip_host": "",
    "sip_port": "5060",
    "sip_username": "",
    "sip_password": "",
    # STT
    "stt_mode": "local",
    "stt_local_model": "base",
    "stt_language": "de",
    "stt_cloud_api_key": "",
    "stt_cloud_base_url": "",
    "stt_cloud_model": "whisper-1",
    # TTS
    "tts_mode": "local",
    "tts_local_model_path": "",
    "tts_local_config_path": "",
    "tts_local_length_scale": "1.0",
    "tts_local_noise_scale": "0.667",
    "tts_local_noise_w": "0.8",
    "tts_cloud_api_key": "",
    "tts_cloud_base_url": "",
    "tts_cloud_model": "tts-1",
    "tts_cloud_voice": "alloy",
    "tts_cloud_speed": "1.0",
    "tts_elevenlabs_api_key": "",
    "tts_elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
    "tts_elevenlabs_model_id": "eleven_multilingual_v2",
    "tts_elevenlabs_stability": "0.5",
    "tts_elevenlabs_similarity": "0.75",
    # TTS Edge (Microsoft, kostenlos)
    "tts_edge_voice": "de-DE-KatjaNeural",
    # TTS Google Cloud TTS
    "tts_google_api_key": "",
    "tts_google_voice": "de-DE-Neural2-A",
    "tts_google_language_code": "de-DE",
    # TTS Azure (Microsoft Azure Speech Service)
    "tts_azure_api_key": "",
    "tts_azure_region": "westeurope",
    "tts_azure_voice": "de-DE-KatjaNeural",
    # LLM
    "llm_mode": "local",
    "llm_local_url": "http://localhost:11434",
    "llm_local_model": "llama3",
    "llm_cloud_api_key": "",
    "llm_cloud_base_url": "",
    "llm_cloud_model": "gpt-4o-mini",
    # Auth / 2FA
    "two_factor_enabled": "false",
    "two_factor_channel": "telegram",
    # Footer
    "footer_text": "AI-Caller",
    "footer_logo_src": "",
    "footer_logo_size": "40",
    "footer_show_version": "true",
    # Agent capabilities
    "agent_web_search_enabled": "false",
    "agent_brave_api_key": "",
    "agent_calendar_enabled": "false",
    "agent_calendar_mode": "readonly",
    "agent_calendar_provider": "none",
    # Google Calendar OAuth2
    "agent_google_client_id": "",
    "agent_google_client_secret": "",
    "agent_google_refresh_token": "",
    "agent_google_calendar_id": "primary",
    # Nextcloud CalDAV
    "agent_nextcloud_url": "",
    "agent_nextcloud_username": "",
    "agent_nextcloud_app_password": "",
    "agent_nextcloud_calendar_name": "personal",
    "agent_nextcloud_owner_email": "",
    "agent_nextcloud_share_users": "[]",
    # Notifications
    "telegram_enabled": "false",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "email_enabled": "false",
    "email_smtp_host": "",
    "email_smtp_port": "465",
    "email_smtp_user": "",
    "email_smtp_pass": "",
    "email_from": "",
    "email_to": "",
    "email_use_ssl": "true",
}
for k, v in DEFAULTS.items():
    if db.get_config(k) is None:
        db.set_config(k, v)


# ── Auto-connect SIP on startup ───────────────────────────────────────────────

def _auto_connect():
    """If SIP credentials are saved, reconnect automatically after startup."""
    import time
    time.sleep(2)  # let gunicorn/eventlet fully initialize first
    cfg = db.get_all_config()
    if (cfg.get("phone_mode") in ("fritzbox", "sip")
            and cfg.get("sip_host")
            and cfg.get("sip_username")
            and cfg.get("sip_password")):
        logger.info("Auto-connecting SIP phone on startup…")
        ok, msg = sip_handler.start_phone(cfg)
        logger.info(f"Auto-connect result: ok={ok} {msg}")

import threading as _threading
_threading.Thread(target=_auto_connect, daemon=True, name="sip-autoconnect").start()


# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    cfg = db.get_all_config()
    user = auth.get_current_user()
    return {
        "current_user": user,
        "app_version": APP_VERSION,
        "footer_text": cfg.get("footer_text", "AI-Caller"),
        "footer_logo_src": cfg.get("footer_logo_src", ""),
        "footer_logo_size": cfg.get("footer_logo_size", "40"),
        "footer_show_version": cfg.get("footer_show_version", "true") == "true",
    }


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    # First-run: no users exist → redirect to setup
    if db.count_users() == 0:
        return redirect(url_for("setup"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_username(username)

        if not user or not auth.verify_password(password, user["password_hash"]) or not user["is_active"]:
            error = "Benutzername oder Passwort falsch."
        else:
            cfg = db.get_all_config()
            two_fa = cfg.get("two_factor_enabled", "false") == "true"
            remember = bool(request.form.get("remember"))

            if two_fa:
                # Store pending login in session, redirect to 2FA page
                session["pending_user_id"] = user["id"]
                session["pending_remember"] = remember
                ok, msg = auth.send_2fa_code(user["id"], cfg)
                if not ok:
                    error = f"2FA-Code konnte nicht gesendet werden: {msg}"
                else:
                    channel = cfg.get("two_factor_channel", "telegram")
                    return redirect(url_for("two_factor", channel=channel))
            else:
                session.permanent = True
                session["user_id"] = user["id"]
                resp = make_response(redirect(request.args.get("next") or url_for("index")))
                if remember:
                    token = auth.create_remember_token(user["id"])
                    resp.set_cookie("ai_caller_remember", token,
                                   max_age=90 * 86400, httponly=True, samesite="Lax")
                return resp

    return render_template("login.html", error=error)


@app.route("/login/2fa", methods=["GET", "POST"])
def two_factor():
    user_id = session.get("pending_user_id")
    if not user_id:
        return redirect(url_for("login"))

    error = None
    channel = request.args.get("channel", "telegram")

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if auth.verify_2fa_code(user_id, code):
            remember = session.pop("pending_remember", False)
            session.pop("pending_user_id", None)
            session.permanent = True
            session["user_id"] = user_id
            resp = make_response(redirect(url_for("index")))
            if remember:
                token = auth.create_remember_token(user_id)
                resp.set_cookie("ai_caller_remember", token,
                               max_age=90 * 86400, httponly=True, samesite="Lax")
            return resp
        else:
            error = "Ungültiger oder abgelaufener Code."

    return render_template("2fa.html", error=error, channel=channel)


@app.route("/logout")
def logout():
    token = request.cookies.get("ai_caller_remember")
    if token:
        db.delete_auth_session(token)
    session.clear()
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie("ai_caller_remember")
    return resp


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db.count_users() > 0:
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if not username:
            error = "Benutzername darf nicht leer sein."
        elif len(password) < 8:
            error = "Passwort muss mindestens 8 Zeichen lang sein."
        elif password != password2:
            error = "Passwörter stimmen nicht überein."
        else:
            db.create_user(username, auth.hash_password(password), role="admin")
            return redirect(url_for("login"))
    return render_template("setup.html", error=error)


# ── User management (admin only) ──────────────────────────────────────────────

@app.route("/admin/users")
@auth.admin_required
def admin_users():
    users = db.list_users()
    status = sip_handler.get_status()
    unread = db.count_unread()
    return render_template("admin/users.html", users=users, unread=unread, status=status)


@app.route("/admin/users/create", methods=["POST"])
@auth.admin_required
def admin_users_create():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    if username and len(password) >= 8:
        db.create_user(username, auth.hash_password(password), role=role)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/edit", methods=["POST"])
@auth.admin_required
def admin_users_edit(user_id):
    kwargs = {}
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    is_active = "1" if request.form.get("is_active") else "0"
    if username:
        kwargs["username"] = username
    if password and len(password) >= 8:
        kwargs["password_hash"] = auth.hash_password(password)
    kwargs["role"] = role
    kwargs["is_active"] = int(is_active)
    db.update_user(user_id, **kwargs)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@auth.admin_required
def admin_users_delete(user_id):
    # Prevent deleting yourself
    if g.current_user["id"] != user_id:
        db.delete_user(user_id)
    return redirect(url_for("admin_users"))


# ── System settings (2FA + footer) ───────────────────────────────────────────

@app.route("/admin/system", methods=["GET", "POST"])
@auth.admin_required
def admin_system():
    cfg = db.get_all_config()
    saved = False
    if request.method == "POST":
        for key in ["two_factor_enabled", "two_factor_channel",
                    "footer_text", "footer_logo_src", "footer_logo_size",
                    "footer_show_version"]:
            val = request.form.get(key)
            if val is not None:
                db.set_config(key, val)
        # Checkboxes (absent = false)
        db.set_config("two_factor_enabled",
                      "true" if request.form.get("two_factor_enabled") else "false")
        db.set_config("footer_show_version",
                      "true" if request.form.get("footer_show_version") else "false")
        cfg = db.get_all_config()
        saved = True
    status = sip_handler.get_status()
    unread = db.count_unread()
    return render_template("admin/system.html", cfg=cfg, saved=saved,
                           unread=unread, status=status)


# ── Inbox ─────────────────────────────────────────────────────────────────────

@app.route("/")
@auth.login_required
def index():
    messages = db.list_inbox(limit=30)
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("inbox.html", messages=messages, unread=unread, status=status)


@app.route("/inbox/<int:inbox_id>")
@auth.login_required
def inbox_detail(inbox_id):
    messages = db.list_inbox()
    entry = next((m for m in messages if m["id"] == inbox_id), None)
    if not entry:
        entry = next((m for m in db.list_inbox(limit=1000) if m["id"] == inbox_id), None)
    if entry:
        db.mark_read(inbox_id)
        call_msgs = db.get_messages(entry["call_id"]) if entry.get("call_id") else []
    else:
        call_msgs = []
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("inbox_detail.html", entry=entry, call_msgs=call_msgs,
                           unread=unread, status=status)


@app.route("/inbox/mark-all-read", methods=["POST"])
@auth.login_required
def mark_all_read():
    db.mark_all_read()
    return redirect(url_for("index"))


@app.route("/inbox/<int:inbox_id>/delete", methods=["POST"])
@auth.login_required
def delete_message(inbox_id):
    db.delete_inbox_entry(inbox_id)
    return redirect(url_for("index"))


@app.route("/inbox/<int:inbox_id>/reply", methods=["POST"])
@auth.login_required
def inbox_reply(inbox_id):
    reply_text = request.form.get("reply_text", "").strip()
    if not reply_text:
        return redirect(url_for("inbox_detail", inbox_id=inbox_id))
    entry = db.get_inbox_entry(inbox_id)
    if not entry:
        return "Not found", 404
    db.save_reply(inbox_id, reply_text, "web")
    cfg = db.get_all_config()
    t = threading.Thread(
        target=sip_handler.make_callback,
        args=(inbox_id, entry["caller_number"], reply_text,
              entry.get("caller_name", ""), cfg),
        daemon=True
    )
    t.start()
    return redirect(url_for("inbox_detail", inbox_id=inbox_id))


@app.route("/api/callback/status/<int:inbox_id>")
@auth.login_required
def api_callback_status(inbox_id):
    entry = db.get_inbox_entry(inbox_id)
    if not entry:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({
        "ok": True,
        "callback_status": entry.get("callback_status", ""),
        "callback_result": entry.get("callback_result", ""),
    })


@app.route("/api/callback/test", methods=["POST"])
@auth.admin_required
def api_callback_test():
    data = request.get_json() or {}
    number = data.get("number", "").strip()
    text = data.get("text", "").strip()
    if not number:
        return jsonify({"ok": False, "error": "Telefonnummer fehlt"}), 400
    if not text:
        return jsonify({"ok": False, "error": "Testansage fehlt"}), 400
    cfg = db.get_all_config()
    t = threading.Thread(
        target=sip_handler.make_test_call,
        args=(number, text, cfg),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "message": f"Test-Anruf an {number} gestartet"})


@app.route("/calls")
@auth.login_required
def calls():
    all_calls = db.list_calls(limit=50)
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("calls.html", calls=all_calls, unread=unread, status=status)


@app.route("/calls/<int:call_id>")
@auth.login_required
def call_detail(call_id):
    call = db.get_call(call_id)
    msgs = db.get_messages(call_id)
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("call_detail.html", call=dict(call) if call else {}, msgs=msgs,
                           unread=unread, status=status)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin")
@auth.admin_required
def admin():
    return redirect(url_for("admin_greeting"))


@app.route("/admin/greeting", methods=["GET", "POST"])
@auth.admin_required
def admin_greeting():
    cfg = db.get_all_config()
    saved = False
    if request.method == "POST":
        keys = [
            "greeting_text", "behavior_prompt", "max_conversation_turns",
            "answer_delay_seconds", "save_to_inbox", "save_recordings",
            "schedule_mode", "schedule_start", "schedule_end", "schedule_days",
            "silence_timeout_seconds", "silence_threshold", "hold_music_enabled",
            "callback_greeting",
        ]
        for key in keys:
            val = request.form.get(key)
            if val is not None:
                db.set_config(key, val)
        # schedule_days comes as multiple values
        days = request.form.getlist("schedule_days_cb")
        if days:
            db.set_config("schedule_days", ",".join(days))
        saved = True
        cfg = db.get_all_config()
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("admin/greeting.html", cfg=cfg, saved=saved, unread=unread, status=status)


@app.route("/admin/phone", methods=["GET", "POST"])
@auth.admin_required
def admin_phone():
    cfg = db.get_all_config()
    alert = None
    if request.method == "POST":
        action = request.form.get("action", "save")
        for key in ["phone_mode", "sip_host", "sip_port", "sip_username", "sip_password"]:
            val = request.form.get(key)
            if val is not None:
                db.set_config(key, val)
        cfg = db.get_all_config()
        if action == "connect":
            # Run in background – never block the HTTP response.
            # SIP registration can take 10–30 s; pyVoIP's start() has a
            # blocking recv() that would otherwise hang the browser tab.
            # Progress is delivered via Socket.IO sip_diag / phone_status events.
            import threading
            t = threading.Thread(
                target=sip_handler.start_phone, args=(cfg,), daemon=True
            )
            t.start()
            alert = {"type": "info",
                     "msg": "Verbindungsaufbau gestartet – "
                            "Ergebnis erscheint in der Live-Diagnose."}
        elif action == "disconnect":
            sip_handler.stop_phone()
            alert = {"type": "success", "msg": "Verbindung getrennt."}
        else:
            alert = {"type": "success", "msg": "Einstellungen gespeichert."}
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("admin/phone.html", cfg=cfg, alert=alert,
                           unread=unread, status=status)


@app.route("/admin/ai", methods=["GET", "POST"])
@auth.admin_required
def admin_ai():
    cfg = db.get_all_config()
    saved = False
    if request.method == "POST":
        keys = [
            "stt_mode", "stt_local_model", "stt_language",
            "stt_cloud_api_key", "stt_cloud_base_url", "stt_cloud_model",
            "tts_mode",
            "tts_local_model_path", "tts_local_config_path",
            "tts_local_length_scale", "tts_local_noise_scale", "tts_local_noise_w",
            "tts_cloud_api_key", "tts_cloud_base_url", "tts_cloud_model",
            "tts_cloud_voice", "tts_cloud_speed",
            "tts_elevenlabs_api_key", "tts_elevenlabs_voice_id", "tts_elevenlabs_model_id",
            "tts_elevenlabs_stability", "tts_elevenlabs_similarity",
            "tts_edge_voice",
            "tts_google_api_key", "tts_google_voice", "tts_google_language_code",
            "tts_azure_api_key", "tts_azure_region", "tts_azure_voice",
            "llm_mode", "llm_local_url", "llm_local_model",
            "llm_cloud_api_key", "llm_cloud_base_url", "llm_cloud_model",
        ]
        for key in keys:
            val = request.form.get(key)
            if val is not None:
                db.set_config(key, val)
        saved = True
        cfg = db.get_all_config()
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("admin/ai.html", cfg=cfg, saved=saved, unread=unread, status=status)


@app.route("/admin/agent", methods=["GET", "POST"])
@auth.admin_required
def admin_agent():
    cfg = db.get_all_config()
    saved = False
    if request.method == "POST":
        keys = [
            "agent_web_search_enabled", "agent_brave_api_key",
            "agent_calendar_enabled", "agent_calendar_mode", "agent_calendar_provider",
            "agent_google_client_id", "agent_google_client_secret",
            "agent_google_refresh_token", "agent_google_calendar_id",
            "agent_nextcloud_url", "agent_nextcloud_username",
            "agent_nextcloud_app_password", "agent_nextcloud_calendar_name",
            "agent_nextcloud_owner_email", "agent_nextcloud_share_users",
        ]
        for key in keys:
            val = request.form.get(key)
            if val is not None:
                db.set_config(key, val)
        saved = True
        cfg = db.get_all_config()
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("admin/agent.html", cfg=cfg, saved=saved, unread=unread, status=status)


@app.route("/admin/agent/google-callback")
@auth.admin_required
def admin_agent_google_callback():
    """Handle Google OAuth2 callback and exchange code for refresh token."""
    code = request.args.get("code", "")
    error = request.args.get("error", "")
    if error:
        return render_template("admin/agent.html", cfg=db.get_all_config(), saved=False,
                               unread=db.count_unread(), status=sip_handler.get_status(),
                               oauth_error=f"OAuth-Fehler: {error}")
    cfg = db.get_all_config()
    redirect_uri = request.host_url.rstrip("/") + url_for("admin_agent_google_callback")
    try:
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": cfg.get("agent_google_client_id", ""),
            "client_secret": cfg.get("agent_google_client_secret", ""),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        refresh_token = data.get("refresh_token", "")
        if refresh_token:
            db.set_config("agent_google_refresh_token", refresh_token)
        return redirect(url_for("admin_agent") + "?oauth=ok")
    except Exception as e:
        cfg = db.get_all_config()
        return render_template("admin/agent.html", cfg=cfg, saved=False,
                               unread=db.count_unread(), status=sip_handler.get_status(),
                               oauth_error=f"Token-Austausch fehlgeschlagen: {e}")


@app.route("/api/agent/test-connection", methods=["POST"])
@auth.admin_required
def api_agent_test_connection():
    from agent_tools import _get_google_events, _get_nextcloud_events, _fetch_nextcloud_events_raw, _format_nc_date
    data = request.get_json() or {}
    provider = data.get("provider", "")
    cfg = db.get_all_config()
    try:
        if provider == "google":
            if not cfg.get("agent_google_refresh_token"):
                return jsonify({"ok": False, "error": "Kein Refresh-Token. Bitte zuerst OAuth2-Flow abschließen."})
            result = _get_google_events(3, 0, cfg)
            return jsonify({"ok": True, "msg": f"Google Calendar erreichbar. {result[:120]}"})
        elif provider == "nextcloud":
            if not cfg.get("agent_nextcloud_url") or not cfg.get("agent_nextcloud_username"):
                return jsonify({"ok": False, "error": "URL und Benutzername erforderlich."})
            raw_events = _fetch_nextcloud_events_raw(7, 0, cfg)
            events_out = []
            for e in raw_events:
                dtstart = e.get("start", "")
                dtend   = e.get("end", "")
                all_day = len(dtstart) == 8  # VALUE=DATE format: 20250415
                events_out.append({
                    "summary":     e.get("summary", ""),
                    "start":       _format_nc_date(dtstart),
                    "end":         _format_nc_date(dtend),
                    "all_day":     all_day,
                    "location":    e.get("location", ""),
                    "description": e.get("description", ""),
                })
            return jsonify({
                "ok": True,
                "msg": f"Nextcloud CalDAV erreichbar. {len(events_out)} Termin(e) in den nächsten 7 Tagen.",
                "events": events_out,
                "calendar": cfg.get("agent_nextcloud_calendar_name", "personal"),
            })
        else:
            return jsonify({"ok": False, "error": "Unbekannter Anbieter."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/admin/notifications", methods=["GET", "POST"])
@auth.admin_required
def admin_notifications():
    cfg = db.get_all_config()
    saved = False
    test_result = {}
    if request.method == "POST":
        action = request.form.get("action", "save")
        keys = [
            "telegram_enabled", "telegram_bot_token", "telegram_chat_id",
            "email_enabled", "email_smtp_host", "email_smtp_port",
            "email_smtp_user", "email_smtp_pass", "email_from", "email_to", "email_use_ssl",
        ]
        for key in keys:
            val = request.form.get(key)
            if val is not None:
                db.set_config(key, val)
        cfg = db.get_all_config()
        if action == "test_telegram":
            ok, result = test_telegram(cfg.get("telegram_bot_token", ""), cfg.get("telegram_chat_id", ""))
            test_result = {"type": "telegram", "ok": ok, "msg": result}
        elif action == "test_email":
            ok, result = test_email(
                cfg.get("email_smtp_host", ""),
                int(cfg.get("email_smtp_port", 465)),
                cfg.get("email_smtp_user", ""),
                cfg.get("email_smtp_pass", ""),
                cfg.get("email_from", cfg.get("email_smtp_user", "")),
                cfg.get("email_to", ""),
                cfg.get("email_use_ssl", "true").lower() == "true"
            )
            test_result = {"type": "email", "ok": ok, "msg": result}
        else:
            saved = True
    unread = db.count_unread()
    status = sip_handler.get_status()
    return render_template("admin/notifications.html", cfg=cfg, saved=saved,
                           test_result=test_result, unread=unread, status=status)


# ── Telegram API ──────────────────────────────────────────────────────────────

@app.route("/api/telegram/get-chat-id", methods=["POST"])
@auth.admin_required
def api_telegram_get_chat_id():
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Kein Bot-Token angegeben"})
    ok, chat_id, name_or_error = get_telegram_chat_id(token)
    if ok:
        return jsonify({"ok": True, "chat_id": chat_id, "name": name_or_error})
    return jsonify({"ok": False, "error": name_or_error})


@app.route("/api/telegram/test", methods=["POST"])
@auth.admin_required
def api_telegram_test():
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    chat_id = data.get("chat_id", "").strip()
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Token oder Chat-ID fehlt"})
    ok, msg, _ = send_telegram(token, chat_id, "✅ AI-Caller Telegram-Test erfolgreich!")
    if ok:
        return jsonify({"ok": True, "msg": "Testnachricht erfolgreich gesendet."})
    return jsonify({"ok": False, "error": msg})


@app.route("/api/email/test", methods=["POST"])
@auth.admin_required
def api_email_test():
    from notifications import test_email as _test_email
    data = request.get_json() or {}
    smtp_host = data.get("email_smtp_host", "").strip()
    smtp_port = int(data.get("email_smtp_port", 465) or 465)
    smtp_user = data.get("email_smtp_user", "").strip()
    smtp_pass = data.get("email_smtp_pass", "").strip()
    from_addr = data.get("email_from", smtp_user).strip() or smtp_user
    to_addr = data.get("email_to", "").strip()
    use_tls = str(data.get("email_use_ssl", "true")).lower() == "true"
    if not all([smtp_host, smtp_user, smtp_pass, to_addr]):
        return jsonify({"ok": False, "error": "SMTP-Einstellungen unvollständig"})
    ok, msg = _test_email(smtp_host, smtp_port, smtp_user, smtp_pass, from_addr, to_addr, use_tls)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": msg})


# ── Telegram recipients API ───────────────────────────────────────────────────

@app.route("/api/telegram/recipients", methods=["GET"])
@auth.admin_required
def api_telegram_recipients_list():
    return jsonify({"ok": True, "recipients": db.list_telegram_recipients()})


@app.route("/api/telegram/recipients", methods=["POST"])
@auth.admin_required
def api_telegram_recipients_add():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    token = data.get("bot_token", "").strip()
    chat_id = data.get("chat_id", "").strip()
    if not name or not token or not chat_id:
        return jsonify({"ok": False, "error": "Name, Token und Chat-ID erforderlich"})
    rid = db.create_telegram_recipient(name, token, chat_id)
    return jsonify({"ok": True, "id": rid})


@app.route("/api/telegram/recipients/<int:rid>", methods=["DELETE"])
@auth.admin_required
def api_telegram_recipients_delete(rid):
    db.delete_telegram_recipient(rid)
    return jsonify({"ok": True})


@app.route("/api/telegram/recipients/<int:rid>/test", methods=["POST"])
@auth.admin_required
def api_telegram_recipients_test(rid):
    recipients = db.list_telegram_recipients()
    r = next((x for x in recipients if x["id"] == rid), None)
    if not r:
        return jsonify({"ok": False, "error": "Empfänger nicht gefunden"})
    from notifications import send_telegram
    ok, msg, _ = send_telegram(r["bot_token"], r["chat_id"], "✅ AI-Caller Telegram-Test erfolgreich!")
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": msg})


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/status")
@auth.login_required
def api_status():
    return jsonify({
        "phone": sip_handler.get_status(),
        "unread": db.count_unread(),
        "total_calls": db.count_calls(),
    })


@app.route("/api/activity-log")
@auth.login_required
def api_activity_log():
    return jsonify(sip_handler.get_activity_log())


@app.route("/api/phone/start", methods=["POST"])
@auth.admin_required
def api_phone_start():
    cfg = db.get_all_config()
    ok, msg = sip_handler.start_phone(cfg)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/phone/stop", methods=["POST"])
@auth.admin_required
def api_phone_stop():
    sip_handler.stop_phone()
    return jsonify({"ok": True})


@app.route("/api/fritzbox/scan", methods=["POST"])
@auth.admin_required
def api_fritzbox_scan():
    from fritzbox_setup import discover_fritzbox
    try:
        found = discover_fritzbox()
        return jsonify({"ok": True, "found": found})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "found": []})


@app.route("/api/fritzbox/clients", methods=["POST"])
@auth.admin_required
def api_fritzbox_clients():
    from fritzbox_setup import get_voip_clients
    data = request.get_json() or {}
    ok, msg, clients = get_voip_clients(
        data.get("ip", ""), data.get("password", ""), data.get("username", "admin")
    )
    return jsonify({"ok": ok, "msg": msg, "clients": clients})


@app.route("/api/fritzbox/setup", methods=["POST"])
@auth.admin_required
def api_fritzbox_setup():
    from fritzbox_setup import set_client_password
    data = request.get_json() or {}
    ip          = data.get("ip", "")
    fb_password = data.get("fb_password", "")
    client_idx  = int(data.get("client_index", 0))
    client_id   = data.get("client_id", "")
    sip_password = data.get("sip_password", "")

    ok, msg = set_client_password(ip, fb_password, client_idx, client_id, sip_password)
    if ok:
        db.set_config("phone_mode",    "fritzbox")
        db.set_config("sip_host",      ip)
        db.set_config("sip_port",      "5060")
        db.set_config("sip_username",  client_id)
        db.set_config("sip_password",  sip_password)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/fritzbox/save", methods=["POST"])
@auth.admin_required
def api_fritzbox_save():
    """Save manually confirmed FritzBox credentials (used after manual setup step)."""
    data = request.get_json() or {}
    db.set_config("phone_mode",   "fritzbox")
    db.set_config("sip_host",     data.get("ip", ""))
    db.set_config("sip_port",     "5060")
    db.set_config("sip_username", data.get("client_id", ""))
    db.set_config("sip_password", data.get("sip_password", ""))
    return jsonify({"ok": True})


@app.route("/api/sip/test", methods=["POST"])
@auth.admin_required
def api_sip_test():
    from sip_test import test_sip_register
    data = request.get_json() or {}
    host     = data.get("host", "").strip()
    port     = int(data.get("port", 5060))
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not all([host, username, password]):
        return jsonify({"ok": False, "steps": [
            {"msg": "Host, Benutzername und Passwort erforderlich.", "ok": False}
        ]})

    result = test_sip_register(host, port, username, password, timeout=6.0)

    # Emit each step as a sip_diag event so they appear in the live panel
    for step in result["steps"]:
        level = "success" if step["ok"] is True else (
                "error"   if step["ok"] is False else "info")
        sip_handler._emit_diag(level, step["msg"])

    return jsonify({"ok": result["success"], "steps": result["steps"]})


@app.route("/api/ollama/scan", methods=["POST"])
@auth.admin_required
def api_ollama_scan():
    from ollama_discovery import scan_network
    try:
        found = scan_network()
        return jsonify({"ok": True, "found": found})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "found": []})


@app.route("/api/ollama/test", methods=["POST"])
@auth.admin_required
def api_ollama_test():
    from ollama_discovery import test_connection
    data = request.get_json() or {}
    result = test_connection(
        data.get("url", "").strip() or "http://localhost:11434",
        data.get("model", "").strip() or "llama3"
    )
    return jsonify(result)


@app.route("/api/llm/test-cloud", methods=["POST"])
@auth.admin_required
def api_llm_test_cloud():
    from ollama_discovery import test_cloud_connection
    data = request.get_json() or {}
    result = test_cloud_connection(
        api_key=data.get("api_key", ""),
        base_url=data.get("base_url", ""),
        model=data.get("model", "gpt-4o-mini"),
    )
    return jsonify(result)


# ── Generic config save ───────────────────────────────────────────────────────

@app.route("/api/config/save", methods=["POST"])
@auth.admin_required
def api_config_save():
    """Save arbitrary config keys sent as JSON. Only known keys (in DEFAULTS) are accepted."""
    data = request.get_json() or {}
    allowed = set(DEFAULTS.keys())
    saved = []
    for k, v in data.items():
        if k in allowed:
            db.set_config(k, str(v))
            saved.append(k)
    if not saved:
        return jsonify({"ok": False, "error": "Keine bekannten Schlüssel zum Speichern"}), 400
    return jsonify({"ok": True, "saved": saved})


# ── STT API ───────────────────────────────────────────────────────────────────

@app.route("/api/stt/test", methods=["POST"])
@auth.admin_required
def api_stt_test():
    """Test STT by synthesizing a short phrase with espeak and transcribing it."""
    import ai_engine as _ae
    import subprocess, tempfile, os as _os
    data = request.get_json() or {}
    # Build cfg from posted values (override current DB config)
    cfg = dict(db.get_all_config())
    cfg.update({k: v for k, v in data.items()})
    test_text = "Hallo, das ist ein Test."
    # Generate test audio via espeak-ng (always available, fast)
    tmp_wav = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            ["espeak-ng", "-v", cfg.get("stt_language") or "de", "-w", tmp_wav, test_text],
            check=True, capture_output=True, timeout=10
        )
        with open(tmp_wav, "rb") as f:
            wav_bytes = f.read()
        result = _ae.transcribe(wav_bytes, cfg)
        return jsonify({"ok": True, "transcription": result, "expected": test_text})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": f"espeak fehlgeschlagen: {e.stderr.decode()}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if _os.path.exists(tmp_wav):
            _os.unlink(tmp_wav)


# ── TTS API ───────────────────────────────────────────────────────────────────

_TTS_MODELS_DIR = os.path.join(os.path.dirname(__file__), "tts_models")

# Curated list of Piper voices available for download
_PIPER_VOICES = [
    # German – alle verfügbaren Stimmen
    {"id": "de_DE-eva_k-x_low",               "lang": "de", "name": "Eva K (DE, x-klein, 21 MB)",          "quality": "x_low",  "size_mb": 21},
    {"id": "de_DE-karlsson-low",              "lang": "de", "name": "Karlsson (DE, niedrig, 63 MB)",        "quality": "low",    "size_mb": 63},
    {"id": "de_DE-kerstin-low",               "lang": "de", "name": "Kerstin ♀ (DE, niedrig, 63 MB)",      "quality": "low",    "size_mb": 63},
    {"id": "de_DE-pavoque-low",               "lang": "de", "name": "Pavoque (DE, niedrig, 63 MB)",        "quality": "low",    "size_mb": 63},
    {"id": "de_DE-ramona-low",                "lang": "de", "name": "Ramona ♀ (DE, niedrig, 63 MB)",       "quality": "low",    "size_mb": 63},
    {"id": "de_DE-thorsten-low",              "lang": "de", "name": "Thorsten (DE, niedrig, 63 MB)",       "quality": "low",    "size_mb": 63},
    {"id": "de_DE-thorsten-medium",           "lang": "de", "name": "Thorsten (DE, mittel, 63 MB)",        "quality": "medium", "size_mb": 63},
    {"id": "de_DE-thorsten-high",             "lang": "de", "name": "Thorsten (DE, hoch, 114 MB) ★",       "quality": "high",   "size_mb": 114},
    {"id": "de_DE-thorsten_emotional-medium", "lang": "de", "name": "Thorsten Emotional (DE, 8 Stimmen)",  "quality": "medium", "size_mb": 77},
    {"id": "de_DE-mls-medium",                "lang": "de", "name": "MLS Multi-Speaker (DE, 236 Stimmen)", "quality": "medium", "size_mb": 77},
    # English US
    {"id": "en_US-amy-medium",                "lang": "en", "name": "Amy ♀ (EN-US, mittel, 63 MB)",        "quality": "medium", "size_mb": 63},
    {"id": "en_US-lessac-medium",             "lang": "en", "name": "Lessac (EN-US, mittel, 63 MB)",       "quality": "medium", "size_mb": 63},
    {"id": "en_US-lessac-high",               "lang": "en", "name": "Lessac (EN-US, hoch, 130 MB) ★",     "quality": "high",   "size_mb": 130},
    {"id": "en_US-libritts_r-medium",         "lang": "en", "name": "LibriTTS (EN-US, mittel, 170 MB)",    "quality": "medium", "size_mb": 170},
    # English GB
    {"id": "en_GB-alan-low",                  "lang": "en", "name": "Alan (EN-GB, niedrig, 27 MB)",        "quality": "low",    "size_mb": 27},
    {"id": "en_GB-alba-medium",               "lang": "en", "name": "Alba ♀ (EN-GB, mittel, 73 MB)",      "quality": "medium", "size_mb": 73},
    # French
    {"id": "fr_FR-siwis-medium",              "lang": "fr", "name": "SIWIS ♀ (FR, mittel, 78 MB)",        "quality": "medium", "size_mb": 78},
    {"id": "fr_FR-upmc-medium",               "lang": "fr", "name": "UPMC (FR, mittel, 78 MB)",           "quality": "medium", "size_mb": 78},
    # Spanish
    {"id": "es_ES-davefx-medium",             "lang": "es", "name": "DaveFX (ES, mittel, 63 MB)",         "quality": "medium", "size_mb": 63},
    {"id": "es_ES-sharvard-medium",           "lang": "es", "name": "Sharvard (ES, mittel, 66 MB)",       "quality": "medium", "size_mb": 66},
    # Italian
    {"id": "it_IT-paola-medium",              "lang": "it", "name": "Paola ♀ (IT, mittel, 61 MB)",        "quality": "medium", "size_mb": 61},
    {"id": "it_IT-riccardo-x_low",            "lang": "it", "name": "Riccardo (IT, x-klein, 22 MB)",      "quality": "x_low",  "size_mb": 22},
]


def _piper_hf_urls(voice_id: str) -> tuple[str, str]:
    """Return (onnx_url, json_url) for a Piper voice from HuggingFace."""
    parts = voice_id.split("-", 2)  # e.g. de_DE, thorsten, low
    locale, name, quality = parts[0], parts[1], parts[2]
    lang = locale.split("_")[0]
    base = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main"
            f"/{lang}/{locale}/{name}/{quality}")
    fname = voice_id
    return f"{base}/{fname}.onnx", f"{base}/{fname}.onnx.json"


@app.route("/api/tts/models")
@auth.admin_required
def api_tts_models():
    """List installed Piper .onnx model files."""
    os.makedirs(_TTS_MODELS_DIR, exist_ok=True)
    models = []
    for fname in sorted(os.listdir(_TTS_MODELS_DIR)):
        if fname.endswith(".onnx"):
            full = os.path.join(_TTS_MODELS_DIR, fname)
            json_path = full + ".json"
            models.append({
                "name": fname[:-5],
                "path": full,
                "config_path": json_path if os.path.exists(json_path) else "",
                "size_mb": round(os.path.getsize(full) / 1_048_576, 1),
            })
    return jsonify({"models": models, "voices": _PIPER_VOICES})


@app.route("/api/tts/download", methods=["POST"])
@auth.admin_required
def api_tts_download():
    """Download a Piper voice model from HuggingFace."""
    data = request.get_json() or {}
    voice_id = data.get("voice_id", "").strip()
    if not voice_id or not any(v["id"] == voice_id for v in _PIPER_VOICES):
        return jsonify({"ok": False, "error": "Unbekannte Voice-ID"}), 400
    os.makedirs(_TTS_MODELS_DIR, exist_ok=True)
    onnx_url, json_url = _piper_hf_urls(voice_id)
    onnx_path = os.path.join(_TTS_MODELS_DIR, f"{voice_id}.onnx")
    json_path = onnx_path + ".json"
    errors = []
    for url, path in [(onnx_url, onnx_path), (json_url, json_path)]:
        try:
            r = requests.get(url, timeout=120, stream=True)
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        except Exception as e:
            if path == onnx_path:
                return jsonify({"ok": False, "error": f"Download fehlgeschlagen: {e}"}), 500
            errors.append(str(e))  # JSON-Config optional
    return jsonify({
        "ok": True, "path": onnx_path,
        "config_path": json_path if os.path.exists(json_path) else "",
        "warnings": errors,
    })


@app.route("/api/tts/preview", methods=["POST"])
@auth.admin_required
def api_tts_preview():
    """Synthesize a short sample text and return WAV audio for browser playback."""
    import ai_engine as _ae
    data = request.get_json() or {}
    text = data.get("text", "Hallo, das ist ein Testtext für die Sprachausgabe.").strip()
    if not text:
        text = "Hallo, das ist ein Testtext."
    # Build a minimal cfg dict from the request payload
    cfg = dict(db.get_all_config())
    cfg.update({k: v for k, v in data.items() if k != "text"})
    try:
        wav = _ae.synthesize(text, cfg)
        return Response(wav, mimetype="audio/wav")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Voice-Test (browser call simulation) ──────────────────────────────────────
# Keyed by call_id (int). Holds conversation state between steps.
_vt_sessions: dict = {}


def _browser_audio_to_wav(data: bytes) -> bytes:
    """Convert browser MediaRecorder audio (WebM/Opus/etc.) to 16kHz mono WAV."""
    import io as _io
    import av as _av
    in_buf = _io.BytesIO(data)
    out_buf = _io.BytesIO()
    with _av.open(in_buf, 'r') as inp:
        with _av.open(out_buf, 'w', format='wav') as out:
            out_stream = out.add_stream('pcm_s16le', rate=16000, layout='mono')
            for frame in inp.decode(audio=0):
                frame.pts = None
                for pkt in out_stream.encode(frame):
                    out.mux(pkt)
            for pkt in out_stream.encode(None):
                out.mux(pkt)
    return out_buf.getvalue()


@app.route("/api/voice-test/start", methods=["POST"])
@auth.admin_required
def api_voice_test_start():
    """Create a test call session, synthesise the greeting, return WAV."""
    cfg = db.get_all_config()
    greeting_text = cfg.get("greeting_text") or (
        "Hallo, Sie haben den KI-Anrufbeantworter erreicht. "
        "Bitte hinterlassen Sie Ihre Nachricht nach dem Ton."
    )
    try:
        greeting_wav = ai_engine.synthesize(greeting_text, cfg)
    except Exception as e:
        return jsonify({"error": f"TTS fehlgeschlagen: {e}"}), 500

    call_id = db.create_call("Testanruf (Browser)", "Browser-Test")
    db.add_message(call_id, "ai", greeting_text)
    _vt_sessions[call_id] = {
        "cfg": cfg,
        "conversation": [],
        "transcript_parts": [f"KI: {greeting_text}"],
        "start_time": time.time(),
    }
    from urllib.parse import quote
    logger.info(f"Voice-test session started, call_id={call_id}")
    return Response(greeting_wav, mimetype="audio/wav",
                    headers={"X-Call-Id": str(call_id),
                             "X-Text": quote(greeting_text[:200])})


@app.route("/api/voice-test/turn", methods=["POST"])
@auth.admin_required
def api_voice_test_turn():
    """Process one user audio turn: STT → LLM → TTS. Return response WAV."""
    try:
        call_id = int(request.headers.get("X-Call-Id", 0))
    except ValueError:
        return jsonify({"error": "Ungültige Call-ID"}), 400

    session = _vt_sessions.get(call_id)
    if not session:
        return jsonify({"error": "Session abgelaufen – bitte neu starten"}), 400

    cfg = session["cfg"]

    audio_data = request.data
    logger.info(f"Voice-test/turn call_id={call_id} audio={len(audio_data)}B "
                f"content-type={request.content_type}")
    if not audio_data:
        return jsonify({"error": "Keine Audiodaten empfangen"}), 400

    # Convert browser audio to WAV
    try:
        wav_bytes = _browser_audio_to_wav(audio_data)
        logger.info(f"Voice-test/turn WAV converted: {len(wav_bytes)}B")
    except Exception as e:
        logger.exception("Voice-test audio conversion failed")
        return jsonify({"error": f"Audio-Konvertierung: {e}"}), 500

    # STT
    try:
        caller_text = ai_engine.transcribe(wav_bytes, cfg)
        logger.info(f"Voice-test/turn STT result: {repr(caller_text)}")
    except Exception as e:
        logger.exception("Voice-test STT failed")
        return jsonify({"error": f"Spracherkennung: {e}"}), 500

    if not caller_text.strip():
        return jsonify({"error": "Kein Text erkannt – bitte deutlicher sprechen"}), 200

    db.add_message(call_id, "user", caller_text)
    session["conversation"].append({"role": "user", "content": caller_text})
    session["transcript_parts"].append(f"Anrufer: {caller_text}")

    # LLM
    behavior_prompt = cfg.get("behavior_prompt") or "Du bist ein freundlicher KI-Assistent."
    try:
        ai_text = ai_engine.generate_response(behavior_prompt, session["conversation"], cfg)
    except Exception as e:
        return jsonify({"error": f"KI-Antwort: {e}"}), 500

    db.add_message(call_id, "ai", ai_text)
    session["conversation"].append({"role": "assistant", "content": ai_text})
    session["transcript_parts"].append(f"KI: {ai_text}")

    # TTS
    try:
        response_wav = ai_engine.synthesize(ai_text, cfg)
    except Exception as e:
        return jsonify({"error": f"Sprachsynthese: {e}"}), 500

    from urllib.parse import quote
    end_call = any(p in ai_text.lower() for p in
                   ["auf wiederhören", "tschüss", "goodbye", "bye", "beende"])
    return Response(response_wav, mimetype="audio/wav",
                    headers={"X-Caller-Text": quote(caller_text[:200]),
                             "X-AI-Text": quote(ai_text[:200]),
                             "X-End-Call": "1" if end_call else "0"})


@app.route("/api/voice-test/end", methods=["POST"])
@auth.admin_required
def api_voice_test_end():
    """Finalise the test call: save summary + inbox entry."""
    data = request.get_json() or {}
    try:
        call_id = int(data.get("call_id", 0))
    except ValueError:
        return jsonify({"error": "Ungültige Call-ID"}), 400

    session = _vt_sessions.pop(call_id, None)
    if not session:
        return jsonify({"ok": True, "summary": ""})

    cfg = session["cfg"]
    duration = int(time.time() - session["start_time"])
    full_transcript = "\n".join(session["transcript_parts"])

    ai_summary = ""
    if full_transcript:
        try:
            ai_summary = ai_engine.summarize_call(full_transcript, cfg)
        except Exception:
            ai_summary = full_transcript[:300]

    db.update_call(call_id,
                   end_time=datetime.utcnow().isoformat(),
                   duration=duration,
                   status="answered",
                   transcript=full_transcript,
                   ai_summary=ai_summary)

    if full_transcript:
        subject = "Testanruf (Browser)"
        body = f"**Zusammenfassung:** {ai_summary}\n\n**Transkript:**\n{full_transcript}"
        inbox_id = db.create_inbox_entry(call_id, subject, body)
        sip_handler._emit("new_inbox", {
            "id": inbox_id, "subject": subject, "caller": "Browser-Test"
        })

    logger.info(f"Voice-test call_id={call_id} ended, duration={duration}s")
    return jsonify({"ok": True, "duration": duration, "summary": ai_summary})


# ── SocketIO ──────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    # Send current status + replay activity log to newly connected client
    status = sip_handler.get_status()
    socketio.emit("status", status)
    for entry in sip_handler.get_activity_log():
        socketio.emit("log_replay", entry)
    # Also emit phone_status so a freshly loaded admin/phone page can
    # immediately show the current connection state without waiting for
    # a state-change event (which already fired before the page loaded).
    socketio.emit("phone_status", {
        "active": status["active"],
        "username": status.get("username", ""),
        "host": status.get("host", ""),
    })


# ── Telegram reply polling thread ─────────────────────────────────────────────

def _telegram_reply_poller():
    """Background thread: polls Telegram every 60 s for replies to notifications.

    When a user replies to a bot notification message, it queues a callback
    to call the original caller back and deliver the reply.
    """
    import database as _db
    from notifications import poll_telegram_replies as _poll
    offset = 0
    logger.info("Telegram reply poller started")
    while True:
        try:
            time.sleep(60)
            cfg = _db.get_all_config()
            if cfg.get("telegram_enabled", "false").lower() != "true":
                continue
            token = cfg.get("telegram_bot_token", "").strip()
            if not token:
                continue
            replies, offset = _poll(token, offset)
            for r in replies:
                inbox_id = r["inbox_id"]
                reply_text = r["reply_text"]
                entry = _db.get_inbox_entry(inbox_id)
                if not entry:
                    continue
                caller_number = entry.get("caller_number", "")
                if not caller_number:
                    continue
                logger.info(f"Telegram reply for inbox #{inbox_id}: queuing callback")
                _db.save_reply(inbox_id, reply_text, "telegram")
                t = threading.Thread(
                    target=sip_handler.make_callback,
                    args=(inbox_id, caller_number, reply_text,
                          entry.get("caller_name", ""), cfg),
                    daemon=True
                )
                t.start()
        except Exception as e:
            logger.error(f"Telegram reply poller error: {e}")


_tg_poller = threading.Thread(target=_telegram_reply_poller, daemon=True,
                               name="tg-reply-poller")
_tg_poller.start()


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)

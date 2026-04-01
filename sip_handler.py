"""
SIP Handler – manages SIP registration and incoming call processing.
"""

import io
import logging
import os
import socket
import threading
import time
import wave
from collections import deque
from datetime import datetime, time as dtime
from typing import Optional

import ai_engine
import database as db
import pyvoip_patch

pyvoip_patch.apply()

logger = logging.getLogger(__name__)

_phone: Optional[object] = None
_phone_lock = threading.Lock()
_active = False
_socketio = None
_desired_active = False
_reconnect_cfg: Optional[dict] = None
_watchdog_thread: Optional[threading.Thread] = None
_calls_active = 0          # number of calls currently in progress
_calls_lock = threading.Lock()

# Live activity log – last 60 events, emitted to new socket clients
_activity_log: deque = deque(maxlen=60)


def set_socketio(sio):
    global _socketio
    _socketio = sio


def _emit(event: str, data: dict):
    """Emit a socket event and append to the activity log."""
    if _socketio:
        try:
            _socketio.emit(event, data)
        except Exception:
            pass
    # Store in activity log for new clients joining later
    _activity_log.append({"event": event, "data": data,
                           "ts": datetime.now().strftime("%H:%M:%S")})


def _emit_diag(level: str, message: str):
    """Emit a SIP diagnostics event (level: info / success / warning / error)."""
    data = {"level": level, "message": message,
            "ts": datetime.now().strftime("%H:%M:%S")}
    if _socketio:
        try:
            _socketio.emit("sip_diag", data)
        except Exception:
            pass
    logger.info(f"SIP [{level}]: {message}")


class _SIPLogForwarder(logging.Handler):
    """Forward pyVoIP log output as sip_diag Socket.IO events."""

    _KEYWORDS_SUCCESS = ("registr", "200", "ok")
    _KEYWORDS_WARN = ("401", "407", "re-reg", "timeout", "retry")
    _KEYWORDS_ERR = ("error", "fail", "exception", "refused", "unreachable")

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
            lower = msg.lower()
            if any(k in lower for k in self._KEYWORDS_ERR):
                lvl = "error"
            elif any(k in lower for k in self._KEYWORDS_WARN):
                lvl = "warning"
            elif any(k in lower for k in self._KEYWORDS_SUCCESS):
                lvl = "success"
            else:
                lvl = "info"
            _emit_diag(lvl, msg)
        except Exception:
            pass


_sip_log_handler = _SIPLogForwarder()
_sip_log_handler.setLevel(logging.DEBUG)


def get_activity_log() -> list:
    return list(_activity_log)


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"


# ── Schedule check ────────────────────────────────────────────────────────────

def is_within_schedule(cfg: dict) -> tuple[bool, str]:
    """
    Check if answering machine should be active right now.
    Returns (active: bool, reason: str).
    """
    mode = cfg.get("schedule_mode", "always")
    if mode == "always":
        return True, "Immer aktiv"

    now = datetime.now()
    weekday = now.weekday()  # 0=Mon … 6=Sun
    day_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

    # Check active days
    days_str = cfg.get("schedule_days", "0,1,2,3,4,5,6")
    try:
        active_days = [int(d) for d in days_str.split(",") if d.strip().isdigit()]
    except Exception:
        active_days = list(range(7))

    if weekday not in active_days:
        active_str = ", ".join(day_names[d] for d in active_days)
        return False, f"Außerhalb der Aktivtage ({active_str})"

    # Check time window
    start_str = cfg.get("schedule_start", "08:00")
    end_str = cfg.get("schedule_end", "20:00")
    try:
        start = dtime.fromisoformat(start_str)
        end = dtime.fromisoformat(end_str)
        now_t = now.time().replace(second=0, microsecond=0)

        if start <= end:
            in_window = start <= now_t <= end
        else:
            # overnight schedule (e.g. 22:00 – 06:00)
            in_window = now_t >= start or now_t <= end

        if not in_window:
            return False, f"Außerhalb der Aktivzeit ({start_str}–{end_str} Uhr)"
        return True, f"Aktiv ({start_str}–{end_str} Uhr)"
    except Exception:
        return True, "Zeitplan-Fehler – immer aktiv"


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _make_beep_wav(freq: int = 880, duration: float = 0.18,
                   sample_rate: int = 16000, amplitude: float = 0.45) -> bytes:
    """Generiert einen kurzen Sinus-Beep als WAV-Bytes (mit Fade-in/out)."""
    import math
    n = int(sample_rate * duration)
    fade = int(sample_rate * 0.015)  # 15 ms Fade
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n):
            env = min(i, n - i, fade) / fade
            s = int(32767 * amplitude * env * math.sin(2 * math.pi * freq * i / sample_rate))
            wf.writeframes(s.to_bytes(2, 'little', signed=True))
    return buf.getvalue()


_BEEP_WAV = _make_beep_wav()  # high 880 Hz → "start speaking"


def _make_end_beep_wav() -> bytes:
    """Two descending tones (660 Hz → 440 Hz) – signals end of recording."""
    import math
    sample_rate = 16000
    notes = [(660, 0.12), (440, 0.14)]
    pause = int(sample_rate * 0.04)
    fade = int(sample_rate * 0.012)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for freq, dur in notes:
            n = int(sample_rate * dur)
            for i in range(n):
                env = min(i, n - i, fade) / fade
                s = int(32767 * 0.4 * env * math.sin(2 * math.pi * freq * i / sample_rate))
                wf.writeframes(s.to_bytes(2, 'little', signed=True))
            for _ in range(pause):
                wf.writeframes((0).to_bytes(2, 'little', signed=True))
    return buf.getvalue()


_END_BEEP_WAV = _make_end_beep_wav()  # descending 660→440 Hz → "got it, processing"


def _make_hold_wav() -> bytes:
    """Generates a simple 4-note hold melody as WAV bytes (16-bit @ 16 kHz)."""
    import math
    sample_rate = 16000
    # Ascending pentatonic notes: C5, E5, G5, A5
    notes = [(523, 0.35), (659, 0.35), (784, 0.35), (880, 0.45)]
    pause_samples = int(sample_rate * 0.06)
    fade = int(sample_rate * 0.012)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for freq, dur in notes:
            n = int(sample_rate * dur)
            for i in range(n):
                env = min(i, n - i, fade) / fade
                s = int(32767 * 0.65 * env * math.sin(2 * math.pi * freq * i / sample_rate))
                wf.writeframes(s.to_bytes(2, 'little', signed=True))
            for _ in range(pause_samples):
                wf.writeframes((0).to_bytes(2, 'little', signed=True))
    return buf.getvalue()


_HOLD_WAV = _make_hold_wav()

CHUNK_SIZE = 160  # 160 Samples × 1 Byte (8-bit Offset-PCM) = 20 ms @ 8 kHz
# pyVoIP dekodiert A-law intern auf 8-bit (alaw2lin width=1).
# Nach bias(-128) hat Sprache typisch RMS 2–15 (8-bit-Raum), Stille RMS=0.
SILENCE_THRESHOLD = 2
SILENCE_TIMEOUT = 100      # 100 × 20 ms = 2 s Pause nach Sprache → Ende
MAX_RECORD_CHUNKS = 1500     # max. 30 s Sprechzeit pro Runde (nach Sprachbeginn)
PRE_SPEECH_TIMEOUT_S = 10.0  # real-world seconds to wait for caller to begin speaking
SPEECH_CONFIRM_CHUNKS = 5    # 5 × 20 ms = 100 ms consecutive non-silent chunks
                             # required before speech_started is set — prevents
                             # beep echo / line noise from triggering recording


def _record_until_silence(call, silence_threshold: int = SILENCE_THRESHOLD,
                          silence_timeout: int = SILENCE_TIMEOUT) -> bytes:
    """Record caller audio until post-speech silence.

    Pre-speech silence is NOT buffered, so Whisper only receives real speech
    (prevents hallucinations like "Vielen Dank." on silent audio).

    SPEECH_CONFIRM_CHUNKS consecutive non-silent chunks are required before
    recording is considered started — beep echo (1-2 chunks) is ignored.

    The pre-speech timeout is wall-clock based so stale buffered packets
    (from during AI playback) drain instantly without eating real time.
    """
    from pyVoIP.VoIP import CallState
    buffer = bytearray()
    silence_count = 0
    total_chunks = 0
    speech_started = False
    consecutive_speech = 0
    pre_speech_deadline = time.monotonic() + PRE_SPEECH_TIMEOUT_S
    pending_chunks: list = []  # confirmation chunks not yet flushed to buffer

    while call.state == CallState.ANSWERED and total_chunks < MAX_RECORD_CHUNKS:
        chunk = call.readAudio(CHUNK_SIZE)
        if chunk:
            is_silent = ai_engine.detect_silence(bytes(chunk), silence_threshold)

            if not speech_started:
                if not is_silent:
                    consecutive_speech += 1
                    pending_chunks.append(bytes(chunk))
                    if consecutive_speech >= SPEECH_CONFIRM_CHUNKS:
                        speech_started = True
                        buffer.extend(b"".join(pending_chunks))  # flush onset
                        pending_chunks = []
                else:
                    consecutive_speech = 0
                    pending_chunks = []  # reset on any silent chunk

                if time.monotonic() > pre_speech_deadline:
                    break  # Caller didn't speak within timeout
                continue

            # Speech confirmed – buffer and apply post-speech silence detection.
            buffer.extend(chunk)
            silence_count = silence_count + 1 if is_silent else 0
            if silence_count >= silence_timeout:
                break
            total_chunks += 1
        else:
            time.sleep(0.02)

    if not speech_started:
        return b""  # No speech detected – prevents STT hallucination
    return bytes(buffer)


def _play_wav_to_call(call, wav_bytes: bytes):
    from pyVoIP.VoIP import CallState
    mulaw_data = ai_engine.wav_bytes_to_mulaw(wav_bytes, target_rate=8000)
    offset = 0
    # 160 samples @ 8000 Hz = exactly 20 ms per chunk; use monotonic clock for accuracy
    next_send = time.monotonic()
    while call.state == CallState.ANSWERED and offset < len(mulaw_data):
        call.writeAudio(mulaw_data[offset:offset + CHUNK_SIZE])
        call.readAudio(CHUNK_SIZE, False)  # drain incoming buffer to prevent backlog
        offset += CHUNK_SIZE
        next_send += 0.020
        sleep_for = next_send - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


def _play_hold_music_loop(call, stop_event: threading.Event):
    """Play hold music in a loop until stop_event is set."""
    from pyVoIP.VoIP import CallState
    mulaw_data = ai_engine.wav_bytes_to_mulaw(_HOLD_WAV, target_rate=8000)
    offset = 0
    next_send = time.monotonic()
    while not stop_event.is_set() and call.state == CallState.ANSWERED:
        chunk = mulaw_data[offset:offset + CHUNK_SIZE]
        if len(chunk) < CHUNK_SIZE:
            chunk = chunk + b'\x80' * (CHUNK_SIZE - len(chunk))
        call.writeAudio(chunk)
        call.readAudio(CHUNK_SIZE, False)  # drain incoming buffer to prevent backlog
        offset += CHUNK_SIZE
        if offset >= len(mulaw_data):
            offset = 0  # loop
        next_send += 0.020
        sleep_for = next_send - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


# ── Call handler ──────────────────────────────────────────────────────────────

def _handle_call(call):
    from pyVoIP.VoIP import CallState

    # Parse caller info
    caller_raw = call.request.headers.get("From", {})
    if isinstance(caller_raw, dict):
        caller_number = caller_raw.get("number") or caller_raw.get("address", "Unbekannt")
        caller_name = (caller_raw.get("caller") or "").strip('" ')
    else:
        caller_number = str(caller_raw)
        caller_name = ""

    call_id = db.create_call(caller_number, caller_name)
    cfg = db.get_all_config()

    _emit("call_ringing", {
        "call_id": call_id,
        "caller": caller_number,
        "caller_name": caller_name,
        "time": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"Incoming call #{call_id} from {caller_number}")

    # Schedule check
    active, reason = is_within_schedule(cfg)
    if not active:
        _emit("call_rejected", {
            "call_id": call_id,
            "caller": caller_number,
            "reason": reason,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        db.update_call(call_id, status="rejected",
                       end_time=datetime.utcnow().isoformat(), duration=0)
        try:
            call.hangup()
        except Exception:
            pass
        return

    greeting_text = cfg.get("greeting_text",
        "Hallo, Sie haben den KI-Anrufbeantworter erreicht. "
        "Bitte hinterlassen Sie Ihre Nachricht nach dem Signal.")
    behavior_prompt = cfg.get("behavior_prompt",
        "Du bist ein freundlicher KI-Anrufbeantworter. Nimm die Nachricht entgegen.")
    max_turns = int(cfg.get("max_conversation_turns", 3))
    save_to_inbox = cfg.get("save_to_inbox", "true").lower() == "true"

    conversation = []
    full_transcript_parts = []

    try:
        answer_delay = float(cfg.get("answer_delay_seconds", "2"))
        time.sleep(answer_delay)
        call.answer()
        time.sleep(0.3)

        _emit("call_answered", {
            "call_id": call_id,
            "caller": caller_number,
            "caller_name": caller_name,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

        greeting_wav = ai_engine.synthesize(greeting_text, cfg)
        _play_wav_to_call(call, greeting_wav)
        _play_wav_to_call(call, _BEEP_WAV)
        db.add_message(call_id, "ai", greeting_text)
        _emit("call_transcript", {
            "call_id": call_id,
            "direction": "ai",
            "text": greeting_text,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

        start_time = time.time()

        # VAD / hold-music settings from config
        silence_timeout_s = float(cfg.get("silence_timeout_seconds", "3.0"))
        silence_threshold_val = int(cfg.get("silence_threshold", "2"))
        silence_timeout_chunks = max(1, int(silence_timeout_s * 50))  # 50 chunks/s @ 20 ms
        hold_music_enabled = cfg.get("hold_music_enabled", "true").lower() == "true"

        for turn in range(max_turns):
            if call.state != CallState.ANSWERED:
                break

            mulaw_audio = _record_until_silence(call, silence_threshold_val, silence_timeout_chunks)
            if not mulaw_audio or len(mulaw_audio) < CHUNK_SIZE * 5:
                break

            # Signal end of recording – caller hears descending double-beep
            if call.state == CallState.ANSWERED:
                _play_wav_to_call(call, _END_BEEP_WAV)

            # Start hold music during STT / LLM / TTS processing
            _stop_hold = threading.Event()
            _hold_thread = None
            if hold_music_enabled and call.state == CallState.ANSWERED:
                _hold_thread = threading.Thread(
                    target=_play_hold_music_loop,
                    args=(call, _stop_hold), daemon=True)
                _hold_thread.start()

            response_wav = None
            ai_text = ""
            try:
                _emit("call_processing", {
                    "call_id": call_id,
                    "step": "stt",
                    "time": datetime.now().strftime("%H:%M:%S"),
                })

                wav_bytes = ai_engine.mulaw_to_wav_bytes(mulaw_audio)
                # DEBUG: empfangenes Audio speichern für Diagnose
                try:
                    import audioop as _ao
                    raw_rms = _ao.rms(_ao.bias(mulaw_audio, 1, -128), 1)
                    logger.info(f"STT-Audio: {len(mulaw_audio)} Bytes raw, RMS={raw_rms}, WAV={len(wav_bytes)} Bytes")
                    with open("/tmp/stt_debug.wav", "wb") as _f:
                        _f.write(wav_bytes)
                except Exception as _e:
                    logger.warning(f"Debug-Dump fehlgeschlagen: {_e}")
                try:
                    caller_text = ai_engine.transcribe(wav_bytes, cfg)
                except Exception as e:
                    logger.error(f"STT error: {e}")
                    caller_text = "[Transkription fehlgeschlagen]"

                if not caller_text.strip():
                    break

                db.add_message(call_id, "caller", caller_text)
                full_transcript_parts.append(f"Anrufer: {caller_text}")
                conversation.append({"role": "user", "content": caller_text})
                _emit("call_transcript", {
                    "call_id": call_id,
                    "direction": "caller",
                    "text": caller_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })

                if call.state != CallState.ANSWERED:
                    break

                _emit("call_processing", {
                    "call_id": call_id,
                    "step": "llm",
                    "time": datetime.now().strftime("%H:%M:%S"),
                })

                try:
                    ai_text = ai_engine.generate_response(behavior_prompt, conversation, cfg)
                except Exception as e:
                    logger.error(f"LLM error: {e}")
                    ai_text = "Entschuldigung, ich konnte Ihre Anfrage nicht verarbeiten. Auf Wiederhören."

                conversation.append({"role": "assistant", "content": ai_text})
                db.add_message(call_id, "ai", ai_text)
                full_transcript_parts.append(f"KI: {ai_text}")
                _emit("call_transcript", {
                    "call_id": call_id,
                    "direction": "ai",
                    "text": ai_text,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })

                try:
                    response_wav = ai_engine.synthesize(ai_text, cfg)
                except Exception as e:
                    logger.error(f"TTS error: {e}")
            finally:
                _stop_hold.set()
                if _hold_thread is not None:
                    _hold_thread.join(timeout=0.5)

            if response_wav is None:
                break

            _play_wav_to_call(call, response_wav)
            _play_wav_to_call(call, _BEEP_WAV)

            if any(p in ai_text.lower() for p in
                   ["auf wiederhören", "tschüss", "goodbye", "bye", "beende"]):
                time.sleep(0.5)
                break

        duration = int(time.time() - start_time)
        full_transcript = "\n".join(full_transcript_parts)

        ai_summary = ""
        if full_transcript:
            try:
                ai_summary = ai_engine.summarize_call(full_transcript, cfg)
            except Exception as e:
                ai_summary = full_transcript[:300]

        db.update_call(call_id,
            end_time=datetime.utcnow().isoformat(),
            duration=duration,
            status="answered",
            transcript=full_transcript,
            ai_summary=ai_summary)

        _emit("call_ended", {
            "call_id": call_id,
            "caller": caller_number,
            "caller_name": caller_name,
            "duration": duration,
            "summary": ai_summary[:120] if ai_summary else "",
            "time": datetime.now().strftime("%H:%M:%S"),
        })

        if save_to_inbox and full_transcript:
            subject = f"Anruf von {caller_number}" + (f" ({caller_name})" if caller_name else "")
            body = f"**Zusammenfassung:** {ai_summary}\n\n**Transkript:**\n{full_transcript}"
            inbox_id = db.create_inbox_entry(call_id, subject, body)
            _emit("new_inbox", {"id": inbox_id, "subject": subject, "caller": caller_number})
            try:
                from notifications import send_notifications
                send_notifications(subject, body, caller_number, cfg, inbox_id=inbox_id)
            except Exception as e:
                logger.error(f"Notification error: {e}")

    except Exception as e:
        logger.exception(f"Call #{call_id} error: {e}")
        db.update_call(call_id, status="error", end_time=datetime.utcnow().isoformat())
        _emit("call_error", {
            "call_id": call_id,
            "caller": caller_number,
            "error": str(e)[:120],
            "time": datetime.now().strftime("%H:%M:%S"),
        })
    finally:
        global _calls_active
        with _calls_lock:
            _calls_active = max(0, _calls_active - 1)
        try:
            call.hangup()
        except Exception:
            pass


def _call_callback(call):
    global _calls_active
    with _calls_lock:
        _calls_active += 1
    t = threading.Thread(target=_handle_call, args=(call,), daemon=True)
    t.start()


# ── Watchdog / auto-reconnect ─────────────────────────────────────────────────

def _check_sip_registered(phone) -> bool:
    """
    Return True if the SIP client reports a healthy REGISTERED state.
    Returns True on exception (can't determine → assume ok) to avoid
    false positives that would trigger unnecessary reconnects.
    """
    try:
        status = phone.sip.status
        name = status.name if hasattr(status, "name") else str(status)
        return "REGISTERED" in name.upper() and "DE" not in name.upper()
    except Exception:
        return True  # can't tell – assume ok, avoid false reconnect triggers


def _check_router_reachable(host: str, timeout: float = 3.0) -> bool:
    """
    TCP connect to the FritzBox web interface port (80) to verify the router
    is actually reachable at the network level.  Much more reliable than a SIP
    OPTIONS probe because:
      – port 80 is always open on FritzBox
      – no authentication needed
      – TCP connect either succeeds or fails/times out cleanly
    Used by the watchdog to detect silent router outages and reboots where
    pyVoIP's in-memory registration state is stale (still REGISTERED even
    though the router has gone away).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, 80))
        sock.close()
        return True
    except Exception:
        return False


def _watchdog_loop():
    """
    Background thread: monitors SIP registration and reconnects on failure.

    Detection strategy (two independent layers):
      1. pyVoIP status  – every 15 s: checks the in-stack registration flag.
         Catches registration rejections (wrong password, FritzBox reboot
         after the re-REGISTER timer fires, etc.).
      2. TCP ping        – every 60 s: attempts a TCP connect to FritzBox
         port 80.  Detects silent router outages where pyVoIP still thinks
         it is REGISTERED but the network path is gone.

    Safety guard:
      Never reconnects while a call is in progress (_calls_active > 0).
      The watchdog waits until the call finishes, then reconnects.
    """
    global _desired_active, _reconnect_cfg, _active
    logger.info("SIP watchdog started")
    # Give the initial registration time to settle before first check
    time.sleep(20)
    backoff = 10          # seconds before first reconnect attempt
    _ping_ticks = 0
    PING_INTERVAL = 4     # TCP-ping every 4 × 15 s = 60 s
    _router_was_reachable = True

    while _desired_active:
        try:
            with _phone_lock:
                phone = _phone

            # ── No phone object at all ────────────────────────────────────────
            if phone is None:
                if _desired_active:
                    _active = False
                    _emit("phone_status", {"active": False, "username": "", "host": ""})
                    _emit_diag("warning",
                               f"Keine aktive SIP-Verbindung – "
                               f"Neuverbindung in {backoff} s…")
                    time.sleep(backoff)
                    if _desired_active and _reconnect_cfg:
                        _emit_diag("info", "Versuche Neuverbindung zur FritzBox…")
                        ok, msg = start_phone(_reconnect_cfg)
                        backoff = 10 if ok else min(backoff * 2, 300)
                        if not ok:
                            _emit_diag("error", f"Neuverbindung fehlgeschlagen: {msg}")

            # ── Phone object exists – check registration & network ────────────
            else:
                # Layer 1: pyVoIP status (every tick)
                sip_ok = _check_sip_registered(phone)

                # Layer 2: TCP network ping (every PING_INTERVAL ticks)
                _ping_ticks += 1
                if _ping_ticks >= PING_INTERVAL and _reconnect_cfg:
                    _ping_ticks = 0
                    fb_host = _reconnect_cfg.get("sip_host", "")
                    if fb_host:
                        router_up = _check_router_reachable(fb_host)
                        if not router_up and _router_was_reachable:
                            # Router just went away
                            _emit_diag("warning",
                                       f"FritzBox {fb_host} nicht erreichbar – "
                                       f"Router Neustart oder Netzausfall?")
                        elif router_up and not _router_was_reachable:
                            # Router came back – force re-registration
                            _emit_diag("info",
                                       f"FritzBox {fb_host} wieder erreichbar – "
                                       f"baue SIP-Verbindung neu auf…")
                            sip_ok = False  # trigger reconnect below
                        _router_was_reachable = router_up

                # ── Connection lost → reconnect ───────────────────────────────
                if not sip_ok:
                    # Wait for any active call to finish before reconnecting
                    with _calls_lock:
                        active_calls = _calls_active
                    if active_calls > 0:
                        _emit_diag("info",
                                   f"Verbindungsproblem erkannt – warte auf "
                                   f"Ende des laufenden Gesprächs…")
                        time.sleep(15)
                        continue

                    try:
                        status_name = phone.sip.status.name
                    except Exception:
                        status_name = "unbekannt"

                    _active = False
                    _emit("phone_status", {"active": False, "username": "", "host": ""})
                    _emit_diag("warning",
                               f"SIP-Registrierung verloren "
                               f"(Status: {status_name}) – "
                               f"Neuverbindung in {backoff} s…")
                    time.sleep(backoff)
                    if _desired_active and _reconnect_cfg:
                        _emit_diag("info", "Versuche Neuverbindung zur FritzBox…")
                        ok, msg = start_phone(_reconnect_cfg)
                        backoff = 10 if ok else min(backoff * 2, 300)
                        if not ok:
                            _emit_diag("error", f"Neuverbindung fehlgeschlagen: {msg}")
                    continue  # skip normal sleep, re-check immediately

                else:
                    backoff = 10  # reset on confirmed healthy heartbeat

        except Exception as e:
            logger.debug(f"SIP watchdog iteration error: {e}")

        time.sleep(15)

    logger.info("SIP watchdog stopped")


# ── Phone management ──────────────────────────────────────────────────────────

def start_phone(cfg: dict) -> tuple[bool, str]:
    global _phone, _active, _desired_active, _reconnect_cfg, _watchdog_thread

    phone_mode = cfg.get("phone_mode", "")
    if phone_mode not in ("fritzbox", "sip"):
        return False, "Keine Verbindungsart gewählt."

    host = cfg.get("sip_host", "").strip()
    port = int(cfg.get("sip_port", 5060))
    username = cfg.get("sip_username", "").strip()
    password = cfg.get("sip_password", "").strip()

    if not all([host, username, password]):
        return False, "SIP-Zugangsdaten unvollständig (Host, Benutzername, Passwort)."

    # Attach pyVoIP log forwarder for real-time diagnostics
    pv_logger = logging.getLogger("pyVoIP")
    if _sip_log_handler not in pv_logger.handlers:
        pv_logger.addHandler(_sip_log_handler)

    # Stop any existing connection first (without clearing _desired_active)
    _stop_phone_internal()
    local_ip = _get_local_ip()

    _emit_diag("info", f"Verbindungsaufbau: {username}@{host}:{port} (lokale IP: {local_ip})")

    try:
        from pyVoIP.VoIP import VoIPPhone

        new_phone = VoIPPhone(
            server=host, port=port,
            username=username, password=password,
            callCallback=_call_callback,
            myIP=local_ip,
            sipPort=5065,
            rtpPortLow=10000, rtpPortHigh=10100
        )

        # pyVoIP's start() blocks on recv() waiting for the SIP 401/200
        # response from the FritzBox.  Run it in a real daemon thread with a
        # hard timeout so we never block the caller (Flask request or watchdog).
        _emit_diag("info", "Sende SIP REGISTER an FritzBox…")
        start_done  = threading.Event()
        start_error = [None]

        def _do_start():
            try:
                new_phone.start()
            except Exception as exc:
                start_error[0] = exc
            finally:
                start_done.set()

        t = threading.Thread(target=_do_start, daemon=True, name="sip-start")
        t.start()
        completed = start_done.wait(timeout=45)

        if start_error[0]:
            raise start_error[0]

        if not completed:
            # start() is still blocking (e.g. FritzBox slow to respond).
            # Accept the phone object anyway – registration may complete shortly.
            _emit_diag("warning",
                       "Registrierung läuft noch (FritzBox antwortet langsam) – "
                       "warte auf Bestätigung…")

        with _phone_lock:
            _phone = new_phone
            _active = True

        _desired_active = True
        _reconnect_cfg = dict(cfg)
        logger.info(f"SIP phone started: {username}@{host}:{port}")
        _emit("phone_status", {"active": True, "username": username, "host": host})
        _emit_diag("success", f"Registriert als {username}@{host} – Anrufbeantworter bereit")

        # Start watchdog if not already running
        if _watchdog_thread is None or not _watchdog_thread.is_alive():
            _watchdog_thread = threading.Thread(
                target=_watchdog_loop, daemon=True, name="sip-watchdog"
            )
            _watchdog_thread.start()

        return True, f"Verbunden als {username}@{host}:{port} (lokale IP: {local_ip})"
    except Exception as e:
        logger.error(f"Failed to start SIP phone: {e}")
        _active = False
        _emit_diag("error", f"Verbindungsfehler: {e}")
        return False, f"Verbindungsfehler: {e}"


def _stop_phone_internal():
    """Stop the phone without touching _desired_active (used by watchdog/reconnect)."""
    global _phone, _active
    with _phone_lock:
        if _phone:
            try:
                _phone.stop()
            except Exception:
                pass
            _phone = None
        _active = False


def stop_phone():
    global _phone, _active, _desired_active
    _desired_active = False
    _stop_phone_internal()
    _emit("phone_status", {"active": False, "username": "", "host": ""})


def is_active() -> bool:
    return _active


def get_status() -> dict:
    cfg = db.get_all_config()
    within, reason = is_within_schedule(cfg)
    with _phone_lock:
        phone = _phone
    # Check actual SIP registration state so the status dot is accurate
    if _active and phone is not None:
        try:
            sip_status = phone.sip.status
            name = sip_status.name if hasattr(sip_status, "name") else str(sip_status)
            actually_active = "REGISTERED" in name.upper() and "DE" not in name.upper()
        except Exception:
            actually_active = _active
    else:
        actually_active = _active
    return {
        "active": actually_active,
        "mode": cfg.get("phone_mode", ""),
        "username": cfg.get("sip_username", ""),
        "host": cfg.get("sip_host", ""),
        "schedule_active": within,
        "schedule_reason": reason,
        "schedule_mode": cfg.get("schedule_mode", "always"),
    }


# ── Outbound callback ──────────────────────────────────────────────────────────

def make_callback(inbox_id: int, caller_number: str, reply_text: str,
                  caller_name: str, cfg: dict):
    """Outbound callback with full conversation loop.

    Flow:
      1. Dial caller_number, wait for answer
      2. Play greeting + reply_text
      3. Full STT→LLM→TTS conversation loop (same as incoming calls)
      4. Append all turns to the original call's message log
    """
    from pyVoIP.VoIP import CallState

    with _phone_lock:
        phone = _phone

    if phone is None:
        db.set_callback_status(inbox_id, "failed", "Keine aktive SIP-Verbindung")
        _emit("callback_status", {"inbox_id": inbox_id, "status": "failed",
                                  "error": "Keine aktive SIP-Verbindung"})
        return

    db.set_callback_status(inbox_id, "calling")
    _emit("callback_status", {
        "inbox_id": inbox_id, "status": "calling",
        "caller": caller_number,
        "time": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"Callback #{inbox_id}: calling {caller_number}")

    # Fetch original call context for LLM history and message log
    inbox_entry = db.get_inbox_entry(inbox_id)
    original_call_id = inbox_entry.get("call_id") if inbox_entry else None
    original_messages = db.get_messages(original_call_id) if original_call_id else []

    call = None
    try:
        logger.info(f"Callback #{inbox_id}: phone.call() starting…")
        call = phone.call(caller_number)
        logger.info(f"Callback #{inbox_id}: phone.call() returned, initial state={call.state}")

        # Wait for answer (max 45 s)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            state_name = call.state.name if hasattr(call.state, "name") else str(call.state)
            logger.debug(f"Callback #{inbox_id}: state={state_name}")
            if "ANSWER" in state_name.upper():
                logger.info(f"Callback #{inbox_id}: call answered")
                break
            if "END" in state_name.upper() or "CANCEL" in state_name.upper():
                logger.info(f"Callback #{inbox_id}: not answered")
                db.set_callback_status(inbox_id, "no_answer", "Anrufer hat nicht abgehoben")
                _emit("callback_status", {"inbox_id": inbox_id, "status": "no_answer"})
                return
            time.sleep(0.5)
        else:
            logger.warning(f"Callback #{inbox_id}: timeout waiting for answer")
            db.set_callback_status(inbox_id, "no_answer", "Timeout – nicht abgehoben")
            _emit("callback_status", {"inbox_id": inbox_id, "status": "no_answer"})
            return

        time.sleep(0.4)

        silence_threshold_val = int(cfg.get("silence_threshold", "2"))
        silence_timeout_s = float(cfg.get("silence_timeout_seconds", "3.0"))
        silence_timeout_chunks = max(1, int(silence_timeout_s * 50))
        max_turns = int(cfg.get("max_conversation_turns", 3))
        hold_music_enabled = cfg.get("hold_music_enabled", "true").lower() == "true"
        behavior_prompt = cfg.get("behavior_prompt",
            "Du bist ein freundlicher KI-Anrufbeantworter.")

        # Build greeting
        custom_greeting = cfg.get("callback_greeting", "").strip()
        name_part = f", {caller_name}" if caller_name else ""
        if custom_greeting:
            intro = custom_greeting.replace("{name}", caller_name or "").strip()
        else:
            intro = (f"Hallo{name_part}, hier ist Ihr KI-Anrufbeantworter. "
                     f"Ich habe eine Antwort für Sie.")

        # Mark callback start + play greeting + reply in message log
        if original_call_id:
            db.add_message(original_call_id, "system", "── Rückruf ──")
            db.add_message(original_call_id, "ai", intro)
            db.add_message(original_call_id, "ai", reply_text)

        _play_wav_to_call(call, ai_engine.synthesize(intro, cfg))
        _play_wav_to_call(call, ai_engine.synthesize(reply_text, cfg))
        _play_wav_to_call(call, _BEEP_WAV)

        # Seed conversation history with original call turns + the reply we delivered
        conversation = []
        for msg in original_messages:
            if msg["direction"] == "caller":
                conversation.append({"role": "user", "content": msg["content"]})
            elif msg["direction"] == "ai":
                conversation.append({"role": "assistant", "content": msg["content"]})
        conversation.append({"role": "assistant", "content": reply_text})

        caller_turns = []

        # Full conversation loop
        for turn in range(max_turns):
            if call.state != CallState.ANSWERED:
                break

            mulaw_audio = _record_until_silence(call, silence_threshold_val, silence_timeout_chunks)
            if not mulaw_audio or len(mulaw_audio) < CHUNK_SIZE * 5:
                break

            if call.state == CallState.ANSWERED:
                _play_wav_to_call(call, _END_BEEP_WAV)

            _stop_hold = threading.Event()
            _hold_thread = None
            if hold_music_enabled and call.state == CallState.ANSWERED:
                _hold_thread = threading.Thread(
                    target=_play_hold_music_loop,
                    args=(call, _stop_hold), daemon=True)
                _hold_thread.start()

            response_wav = None
            ai_text = ""
            try:
                wav_bytes = ai_engine.mulaw_to_wav_bytes(mulaw_audio)
                try:
                    caller_text = ai_engine.transcribe(wav_bytes, cfg)
                except Exception as e:
                    logger.error(f"Callback STT error: {e}")
                    caller_text = ""

                if not caller_text.strip():
                    break

                caller_turns.append(caller_text)
                conversation.append({"role": "user", "content": caller_text})
                if original_call_id:
                    db.add_message(original_call_id, "caller", caller_text)
                logger.info(f"Callback #{inbox_id} caller: {caller_text!r}")

                if call.state != CallState.ANSWERED:
                    break

                try:
                    ai_text = ai_engine.generate_response(behavior_prompt, conversation, cfg)
                except Exception as e:
                    logger.error(f"Callback LLM error: {e}")
                    ai_text = "Entschuldigung, ich konnte Ihre Anfrage nicht verarbeiten. Auf Wiederhören."

                conversation.append({"role": "assistant", "content": ai_text})
                if original_call_id:
                    db.add_message(original_call_id, "ai", ai_text)
                logger.info(f"Callback #{inbox_id} ai: {ai_text!r}")

                try:
                    response_wav = ai_engine.synthesize(ai_text, cfg)
                except Exception as e:
                    logger.error(f"Callback TTS error: {e}")
            finally:
                _stop_hold.set()
                if _hold_thread is not None:
                    _hold_thread.join(timeout=0.5)

            if response_wav is None:
                break

            _play_wav_to_call(call, response_wav)
            _play_wav_to_call(call, _BEEP_WAV)

            if any(p in ai_text.lower() for p in
                   ["auf wiederhören", "tschüss", "goodbye", "bye", "beende"]):
                time.sleep(0.5)
                break

        result_text = " | ".join(caller_turns) if caller_turns else "(keine Antwort)"
        db.set_callback_status(inbox_id, "answered", result_text)
        _emit("callback_status", {
            "inbox_id": inbox_id, "status": "answered",
            "result": result_text,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

        if caller_turns:
            try:
                from notifications import send_callback_result_notification
                send_callback_result_notification(inbox_id, result_text, cfg)
            except Exception as e:
                logger.error(f"Callback result notification error: {e}")

    except Exception as e:
        logger.error(f"Callback #{inbox_id} error: {e}")
        db.set_callback_status(inbox_id, "failed", str(e))
        _emit("callback_status", {"inbox_id": inbox_id, "status": "failed",
                                  "error": str(e)[:120]})
    finally:
        if call is not None:
            try:
                call.hangup()
            except Exception:
                pass


def make_test_call(number: str, text: str, cfg: dict):
    """Dial `number`, play `text` via TTS, then hang up. Used for testing."""
    from pyVoIP.VoIP import CallState

    with _phone_lock:
        phone = _phone

    if phone is None:
        logger.error("Test-Anruf: Keine aktive SIP-Verbindung")
        return

    logger.info(f"Test-Anruf: calling {number}")
    call = None
    try:
        logger.info("Test-Anruf: phone.call() starting…")
        call = phone.call(number)
        logger.info(f"Test-Anruf: phone.call() returned, state={call.state}")

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            state_name = call.state.name if hasattr(call.state, "name") else str(call.state)
            if "ANSWER" in state_name.upper():
                logger.info("Test-Anruf: answered")
                break
            if "END" in state_name.upper() or "CANCEL" in state_name.upper():
                logger.info("Test-Anruf: not answered")
                return
            time.sleep(0.5)
        else:
            logger.warning("Test-Anruf: timeout")
            return

        time.sleep(0.4)
        _play_wav_to_call(call, ai_engine.synthesize(text, cfg))
        time.sleep(0.5)
        logger.info("Test-Anruf: playback done, hanging up")

    except Exception as e:
        logger.error(f"Test-Anruf error: {e}")
    finally:
        if call is not None:
            try:
                call.hangup()
            except Exception:
                pass

"""
pyVoIP FritzBox compatibility patch.

Applied automatically when sip_handler.py is imported.

Fixes:
1. parse_raw_header – RFC 3261 compact header expansion (i→Call-ID, v→Via,
   f→From, t→To, m→Contact, l→Content-Length, c→Content-Type …).
   FritzBox latest firmware sends compact forms; pyVoIP 1.6.8 silently drops
   the whole INVITE, so the incoming-call callback never fires.

2. parse_raw_header – split(': ', maxsplit=1) so header values containing
   ': ' (e.g. SIP URIs like <sip:user@host:5060>) are not truncated.
   The original split(': ') also crashes with IndexError on lines without
   a ': ', silently swallowing every incoming call.

3. parse_raw_header – skip empty / malformed lines instead of crashing.

4. SIPClient myIP – resolve '0.0.0.0' → real LAN IP automatically so that
   SIP Via/Contact headers contain a reachable address and FritzBox can
   deliver responses back to us.

Sources:
  https://github.com/tayler6000/pyVoIP/issues/239
  https://github.com/tayler6000/pyVoIP/issues/119
  https://github.com/tayler6000/pyVoIP/issues/167
  https://github.com/tayler6000/pyVoIP/pull/245
"""

import logging
import socket
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

# RFC 3261 §20 compact header name → full name
_COMPACT = {
    "i": "Call-ID",
    "v": "Via",
    "f": "From",
    "t": "To",
    "m": "Contact",
    "l": "Content-Length",
    "c": "Content-Type",
    "e": "Content-Encoding",
    "s": "Subject",
    "k": "Supported",
    "o": "Event",
    "u": "Allow-Events",
    "r": "Refer-To",
    "b": "Referred-By",
    "y": "Identity",
    "n": "Identity-Info",
}


def _get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _patched_parse_raw_header(
    headers_raw: List[bytes],
    handle: Callable[[str, str], None],
) -> None:
    """Robust, RFC 3261-compliant replacement for SIPMessage.parse_raw_header."""
    headers: Dict[str, Any] = {"Via": []}
    prev_key: str = ""

    for x in headers_raw:
        try:
            line = x.decode("utf-8") if isinstance(x, bytes) else str(x)

            # RFC 3261 §7.3.1 folded header (continuation line)
            if line and line[0] in (" ", "\t"):
                if prev_key and prev_key in headers:
                    if isinstance(headers[prev_key], list):
                        headers[prev_key][-1] += " " + line.strip()
                    else:
                        headers[prev_key] = headers[prev_key] + " " + line.strip()
                continue

            line = line.rstrip("\r\n")
            if not line.strip():
                continue  # skip empty lines

            # Split name: value – prefer ': ', fall back to ':'
            if ": " in line:
                key, val = line.split(": ", 1)
            elif ":" in line:
                key, val = line.split(":", 1)
                val = val.strip()
            else:
                continue  # no colon – skip

            # Expand compact header names (RFC 3261 §20)
            key = _COMPACT.get(key, key)
            prev_key = key

            if key == "Via":
                headers["Via"].append(val)
            elif key not in headers:
                headers[key] = val

        except Exception as exc:
            logger.debug(f"pyVoIP patch: skipping malformed header line: {exc}")
            continue

    for k, v in headers.items():
        try:
            handle(k, v)
        except Exception as exc:
            logger.debug(f"pyVoIP patch: error handling header '{k}': {exc}")


_patch_applied = False


def apply():
    """Apply all FritzBox compatibility patches to pyVoIP."""
    global _patch_applied
    if _patch_applied:
        return

    try:
        # Import VoIPPhone FIRST to resolve pyVoIP's internal circular import
        from pyVoIP.VoIP import VoIPPhone  # noqa: F401
        import pyVoIP.SIP as sip_module

        # Patch 1/2/3: robust parse_raw_header
        sip_module.SIPMessage.parse_raw_header = staticmethod(
            _patched_parse_raw_header
        )
        logger.info("pyVoIP patch [1/2]: parse_raw_header (compact headers + robustness)")

        # Patch 4: resolve myIP='0.0.0.0' → real LAN IP + stable registration params
        _orig_init = sip_module.SIPClient.__init__

        def _patched_init(self, server, port, username, password, phone,
                          myIP="0.0.0.0", myPort=5060, *args, **kwargs):
            if not myIP or myIP == "0.0.0.0":
                myIP = _get_lan_ip()
                logger.info(f"pyVoIP patch: myIP auto-resolved → {myIP}")
            _orig_init(self, server, port, username, password, phone,
                       myIP, myPort, *args, **kwargs)
            # FritzBox prefers longer registration intervals (300s standard)
            # Short re-register (120s default) triggers FritzBox 400 Bad Request
            try:
                self.default_expires = 300
            except Exception:
                pass

        sip_module.SIPClient.__init__ = _patched_init
        logger.info("pyVoIP patch [2/3]: SIPClient myIP auto-detection + expires=300")

        # Patch 5: raise REGISTER_FAILURE_THRESHOLD (default 3 is too aggressive)
        # FritzBox transient 4xx errors during firmware activity cause premature shutdown.
        # The variable lives in pyVoIP/__init__.py (module level), NOT on SIPClient.
        try:
            import pyVoIP as _pv
            _pv.REGISTER_FAILURE_THRESHOLD = 10
            logger.info("pyVoIP patch [3/4]: REGISTER_FAILURE_THRESHOLD → 10")
        except Exception as e:
            logger.debug(f"pyVoIP patch: could not raise failure threshold: {e}")

        # Patch 5b: route pyVoIP.debug() (which uses print()) to Python logging
        # so that SIP packet traces appear in sip_diag instead of stdout only.
        try:
            import pyVoIP as _pv2
            _pv_logger = logging.getLogger("pyVoIP")

            def _debug_to_log(s: str):
                _pv_logger.debug(s)

            _pv2.debug = _debug_to_log
            logger.info("pyVoIP patch [3b]: debug() routed to logging")
        except Exception as e:
            logger.debug(f"pyVoIP patch: could not patch debug(): {e}")

        # Patch 6: add recv() timeout to SIPClient sockets so that start() can
        # never block the caller indefinitely.  pyVoIP creates self.s in start()
        # without a timeout, so REGISTER responses could be waited on forever.
        _orig_sip_start = sip_module.SIPClient.start

        def _patched_sip_start(self, *args, **kwargs):
            _orig_sip_start(self, *args, **kwargs)
            # After start() returns (or raises), set a short timeout on the
            # receive socket so the background receive-loop yields regularly.
            try:
                if hasattr(self, "s") and self.s:
                    self.s.settimeout(2)
                    logger.debug("pyVoIP patch: recv socket timeout → 2 s")
            except Exception:
                pass

        sip_module.SIPClient.start = _patched_sip_start
        logger.info("pyVoIP patch [4/4]: SIPClient.start socket timeout")

        # Patch 6b: Replace recv_loop to use settimeout() instead of setblocking().
        #
        # Root cause of outbound call hang: pyVoIP's SIPClient.recv_loop() uses
        # acquired_lock_and_unblocked_socket() which calls setblocking(True/False).
        # setblocking(True) removes ALL socket timeouts (sets timeout=None).
        # Consequence: when invite() acquires recvLock and calls s.recv(8192),
        # the socket is in fully-blocking mode with no timeout.  If FritzBox
        # responds with an unexpected status code (e.g. 403, 404) and nothing
        # follows, the invite() while-loop hangs on the next recv() forever.
        # This also affects SIPClient.parse_message() which also calls
        # setblocking(True) for status responses – same effect inside invite().
        #
        # Fix: replace recv_loop so it uses settimeout(0)/settimeout(10) instead
        # of setblocking(False)/setblocking(True).  This preserves a 10-second
        # timeout on the socket between recv_loop iterations, so invite()'s
        # recv() calls can time out instead of hanging indefinitely.
        try:
            def _patched_recv_loop(self) -> None:
                import socket as _sock
                import time as _time
                while self.NSD:
                    try:
                        with self.recvLock:
                            self.s.settimeout(0)  # non-blocking while holding lock
                            try:
                                self.recv()
                            except _sock.timeout:
                                pass
                            except BlockingIOError:
                                # No data available – re-raise so the outer except
                                # triggers the 10 ms sleep (prevents CPU spin loop).
                                raise
                            except Exception as _recv_exc:
                                logger.debug(f"pyVoIP recv_loop: exception in recv(): {_recv_exc}")
                            finally:
                                # Restore 10 s timeout BEFORE releasing lock so that
                                # invite() sees the timeout as soon as it acquires.
                                self.s.settimeout(10)
                    except BlockingIOError:
                        _time.sleep(0.01)  # yield cooperatively, no data available
                    except Exception:
                        _time.sleep(0.01)

            sip_module.SIPClient.recv_loop = _patched_recv_loop
            logger.info("pyVoIP patch [4b]: recv_loop uses settimeout(10) instead of setblocking")
        except Exception as e:
            logger.warning(f"pyVoIP patch [4b] fehlgeschlagen: {e}")

        # Patch 6c: parse_message also calls self.s.setblocking(True) after handling
        # status responses (200 OK, 404, 503, …).  This removes the timeout that
        # patch 6b sets, so invite()'s recv() calls would still hang when
        # parse_message is called inside invite()'s while-loop.
        # Fix: after the original parse_message, restore settimeout(10).
        try:
            _orig_parse_msg = sip_module.SIPClient.parse_message

            def _patched_parse_message(self, message):
                # Log every SIP message processed by the recv_loop (diagnostics)
                try:
                    status = getattr(message, "status", None)
                    method = getattr(message, "method", None)
                    label = str(status) if status else str(method)
                    logger.info(f"SIP recv_loop: {label}")
                except Exception:
                    pass
                _orig_parse_msg(self, message)
                # Original calls setblocking(True) for response status messages,
                # removing the timeout.  Restore it so invite() can time out.
                try:
                    self.s.settimeout(10)
                except Exception:
                    pass

            sip_module.SIPClient.parse_message = _patched_parse_message
            logger.info("pyVoIP patch [4c]: parse_message restores settimeout(10)")
        except Exception as e:
            logger.warning(f"pyVoIP patch [4c] fehlgeschlagen: {e}")

        # Patch 6d: Replace SIPClient.invite() entirely.
        #
        # Problems with the original invite():
        #  1. When FritzBox sends a final error (403/486/503/…), the while-loop
        #     calls parse_message() and then waits indefinitely for another message
        #     → hangs until socket timeout (10 s), giving no useful error message.
        #  2. 407 Proxy Authentication Required is NOT handled; only 401 is.
        #     FritzBox may use 407 for outbound INVITE authentication.
        #  3. parse_message() inside the loop calls setblocking(True), removing
        #     the socket timeout so the next recv() blocks forever.
        #
        # This replacement:
        #  - Has an explicit 15-second recv() timeout throughout.
        #  - Handles both 401 and 407 with correct Authorization /
        #    Proxy-Authorization headers.
        #  - Breaks immediately on any ≥ 400 response with a clear error.
        #  - Logs every SIP response at INFO level for diagnostics.
        try:
            import socket as _socket_mod

            def _invite_replacement(self, number, ms, sendtype):
                from pyVoIP.SIP import SIPMessage as _SIPMsg
                import socket as _sk
                import hashlib as _hashlib

                call_id = self.gen_call_id()
                sess_id = self.sessID.next()

                def _make_invite(auth_hdr=None, use_branch=None):
                    b = use_branch or ("z9hG4bK" + self.gen_call_id()[0:25])
                    msg = self.gen_invite(
                        number, str(sess_id), ms, sendtype, b, call_id
                    )
                    if auth_hdr:
                        msg = msg.replace(
                            "\r\nContent-Length",
                            f"\r\n{auth_hdr}Content-Length",
                        )
                    return msg

                def _build_auth(resp, proxy=False):
                    # Compute Digest response using the correct INVITE request-URI.
                    # pyVoIP's gen_authorization() uses "sip:server;transport=UDP"
                    # as the URI in HA2, but FritzBox verifies against the actual
                    # request-URI "sip:number@server" — causing a hash mismatch and
                    # silent drop of the authenticated re-INVITE.
                    realm = resp.authentication["realm"]
                    nonce = resp.authentication["nonce"]
                    req_uri = f"sip:{number}@{self.server}"
                    ha1 = _hashlib.md5(
                        f"{self.username}:{realm}:{self.password}".encode("utf8")
                    ).hexdigest()
                    ha2 = _hashlib.md5(
                        f"INVITE:{req_uri}".encode("utf8")
                    ).hexdigest()
                    response = _hashlib.md5(
                        f"{ha1}:{nonce}:{ha2}".encode("utf8")
                    ).hexdigest()
                    hdr = "Proxy-Authorization" if proxy else "Authorization"
                    return (
                        f'{hdr}: Digest username="{self.username}",'
                        f'realm="{realm}",nonce="{nonce}",'
                        f'uri="{req_uri}",'
                        f'response="{response}",algorithm=MD5\r\n'
                    )

                with self.recvLock:
                    # Initial INVITE — generate first branch here
                    first_branch = "z9hG4bK" + self.gen_call_id()[0:25]
                    invite_msg = _make_invite(use_branch=first_branch)
                    self.out.sendto(
                        invite_msg.encode("utf8"), (self.server, self.port)
                    )
                    logger.info(f"SIP: INVITE → sip:{number}@{self.server}")

                    while True:
                        # Always restore timeout before each recv so that
                        # parse_message()'s setblocking(True) can't cause a hang.
                        self.s.settimeout(15)
                        try:
                            raw = self.s.recv(8192)
                        except _sk.timeout:
                            raise RuntimeError(
                                f"SIP INVITE Timeout: FritzBox antwortet nicht "
                                f"auf Anruf zu {number}"
                            )

                        try:
                            resp = _SIPMsg(raw)
                        except Exception as parse_err:
                            logger.warning(f"SIP: Fehler beim Parsen der Antwort: {parse_err}")
                            continue

                        status_code = int(resp.status)
                        resp_call_id = resp.headers.get("Call-ID", "")
                        logger.info(
                            f"SIP: ← {resp.status}  "
                            f"(call-id match={resp_call_id == call_id})"
                        )

                        if resp_call_id != call_id:
                            # Response for a different call — process and skip
                            try:
                                self.parse_message(resp)
                            except Exception:
                                pass
                            continue

                        if not self.NSD:
                            raise RuntimeError("SIP: Phone gestoppt während INVITE")

                        if status_code in (100, 180):
                            # 100 Trying / 180 Ringing → call is being routed
                            return _SIPMsg(invite_msg.encode("utf8")), call_id, sess_id

                        elif status_code in (401, 407):
                            # Authentication required — re-send with credentials
                            proxy = (status_code == 407)
                            logger.info(
                                f"SIP: Auth erforderlich ({status_code}), "
                                f"sende Credentials (proxy={proxy})"
                            )
                            try:
                                ack = self.gen_ack(resp)
                                self.out.sendto(
                                    ack.encode("utf8"), (self.server, self.port)
                                )
                                auth_hdr = _build_auth(resp, proxy=proxy)
                                # RFC 3261 §8.1.3.1: re-INVITE after 401/407 must
                                # use a NEW branch (new transaction, new CSeq).
                                new_branch = "z9hG4bK" + self.gen_call_id()[0:25]
                                logger.info(f"SIP: Auth-URI sip:{number}@{self.server}, neuer Branch {new_branch[:16]}…")
                                invite_msg = _make_invite(auth_hdr, use_branch=new_branch)
                                self.out.sendto(
                                    invite_msg.encode("utf8"),
                                    (self.server, self.port),
                                )
                                logger.info("SIP: re-INVITE mit Auth gesendet")
                                return _SIPMsg(invite_msg.encode("utf8")), call_id, sess_id
                            except Exception as auth_err:
                                raise RuntimeError(
                                    f"SIP Auth fehlgeschlagen ({status_code}): {auth_err}"
                                )

                        elif 200 <= status_code < 300:
                            # Early 2xx — let recv_loop handle it normally
                            try:
                                self.parse_message(resp)
                            except Exception:
                                pass
                            return _SIPMsg(invite_msg.encode("utf8")), call_id, sess_id

                        elif status_code >= 400:
                            # Final error — update call state and fail immediately
                            try:
                                self.parse_message(resp)
                            except Exception:
                                pass
                            raise RuntimeError(
                                f"SIP INVITE abgelehnt: {resp.status}"
                            )

                        else:
                            # Other 1xx provisional — process and continue
                            try:
                                self.parse_message(resp)
                            except Exception:
                                pass

            sip_module.SIPClient.invite = _invite_replacement
            logger.info("pyVoIP patch [4d]: invite() ersetzt (407+4xx+korrekter Digest-URI+neuer Branch)")
        except Exception as e:
            logger.warning(f"pyVoIP patch [4d] fehlgeschlagen: {e}")

        # Patch 7 entfernt: 16-bit-intern-Patch war defekt.
        # pyVoIP bleibt bei 8-bit Offset-PCM (128=Stille). ai_engine konvertiert korrekt.

        # Patch 8: encode_packet PCMA-Bug fix + parse_packet Robustheit
        #
        # Bug 1 (Ausgang): encode_packet ruft für PCMA fälschlicherweise encode_pcmu
        # (µ-law) auf. FritzBox DE verhandelt A-law als ersten Codec.
        # Ergebnis: RTP-Paket hat PT=8 (PCMA) aber µ-law-Inhalt → Rauschen beim Hörer.
        #
        # Bug 2 (Eingang): VoIP.py setzt assoc[8]=UNKNOWN wenn PayloadType("PCMA")
        # scheitert (beim rtpmap-Fallback). Dann liefert RTPMessage.parse() UNKNOWN
        # statt PCMA, und parse_packet() wirft RTPParseError → alle eingehenden
        # PCMA-Pakete werden verworfen → pmin enthält nur Stille → STT bekommt
        # leeres Audio → Whisper halluziniert.
        #
        # Fix: parse_packet prüft den numerischen PT aus dem RTP-Header (Byte 2,
        # Bits 1-7) direkt – unabhängig vom assoc-Mapping.
        try:
            import pyVoIP.RTP as _rtp2
            from pyVoIP.RTP import PayloadType as _PT, RTPParseError as _RTPErr

            def _encode_packet_fixed(self, payload: bytes) -> bytes:
                if self.preference == _PT.PCMU:
                    return self.encode_pcmu(payload)
                elif self.preference == _PT.PCMA:
                    return self.encode_pcma(payload)   # ← Korrektur (vorher: encode_pcmu)
                else:
                    raise _RTPErr("Unsupported codec (encode): " + str(self.preference))

            def _parse_packet_fixed(self, packet: bytes) -> None:
                from pyVoIP.RTP import RTPMessage
                msg = RTPMessage(packet, self.assoc)
                # Numerischen PT direkt aus RTP-Header lesen (Byte 1, Bits 1-7)
                pt_num = packet[1] & 0x7F
                if msg.payload_type == _PT.PCMU or pt_num == 0:
                    self.parse_pcmu(msg)
                elif msg.payload_type == _PT.PCMA or pt_num == 8:
                    self.parse_pcma(msg)
                elif msg.payload_type == _PT.EVENT:
                    self.parse_telephone_event(msg)
                else:
                    raise _RTPErr("Unsupported codec (parse): " + str(msg.payload_type))

            _rtp2.RTPClient.encode_packet = _encode_packet_fixed
            _rtp2.RTPClient.parse_packet = _parse_packet_fixed
            logger.info("pyVoIP patch [5/5]: encode_packet PCMA-Fix + parse_packet Robustheit")
        except Exception as e:
            logger.warning(f"pyVoIP patch [5/5] fehlgeschlagen: {e}")

        _patch_applied = True

    except ImportError:
        # pyVoIP not installed yet – will be applied when it is
        logger.debug("pyVoIP not installed, patch skipped")
    except Exception as e:
        logger.warning(f"pyVoIP patch could not be applied: {e}")

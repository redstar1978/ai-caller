"""
Raw SIP REGISTER test – bypasses pyVoIP completely.

Used for diagnostics: confirms whether FritzBox is reachable and whether
the supplied credentials are accepted, before trying to use pyVoIP.
"""

import hashlib
import select as _select
import socket
import uuid


def test_sip_register(host: str, port: int, username: str, password: str,
                      timeout: float = 5.0) -> dict:
    """
    Send a SIP REGISTER sequence via raw UDP and return step-by-step results.

    Returns:
        {
          "success": bool,
          "steps":   [{"msg": str, "ok": bool|None}, ...],
        }
    """
    steps = []

    def log(msg: str, ok=None):
        steps.append({"msg": msg, "ok": ok})

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        # Bind to OS-assigned port on all interfaces so routing works correctly
        sock.bind(("0.0.0.0", 0))
        my_port = sock.getsockname()[1]

        # Discover the LAN IP that routes to the FritzBox
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((host, port))
            my_ip = probe.getsockname()[0]

        log(f"Lokale Adresse: {my_ip}:{my_port}")
        log(f"Ziel: {host}:{port}")

        call_id = uuid.uuid4().hex + "@" + my_ip
        tag     = uuid.uuid4().hex[:8]

        # ── Step 1: unauthenticated REGISTER ─────────────────────────────────
        branch1  = "z9hG4bK" + uuid.uuid4().hex[:16]
        register1 = "\r\n".join([
            f"REGISTER sip:{host} SIP/2.0",
            f"Via: SIP/2.0/UDP {my_ip}:{my_port};branch={branch1};rport",
            f'From: "{username}" <sip:{username}@{host}>;tag={tag}',
            f'To: "{username}" <sip:{username}@{host}>',
            f"Call-ID: {call_id}",
            "CSeq: 1 REGISTER",
            f"Contact: <sip:{username}@{my_ip}:{my_port}>",
            "Max-Forwards: 70",
            "Expires: 300",
            "Content-Length: 0",
            "\r\n",
        ])

        log(f"Sende REGISTER (ohne Auth) an {host}:{port}…")
        sock.sendto(register1.encode(), (host, port))

        # Wait for response
        ready = _select.select([sock], [], [], timeout)
        if not ready[0]:
            log(
                f"Keine Antwort von {host}:{port} innerhalb {timeout:.0f}s – "
                "mögliche Ursachen: falsche IP/Port, Firewall blockiert UDP, "
                "FritzBox SIP deaktiviert.",
                ok=False,
            )
            return {"success": False, "steps": steps}

        data, addr = sock.recvfrom(4096)
        resp1 = data.decode("utf-8", errors="replace")
        first_line = resp1.split("\r\n")[0]
        log(f"Antwort von {addr[0]}:{addr[1]}: {first_line}", ok=True)

        # ── Step 2: digest auth ───────────────────────────────────────────────
        if "401" in first_line or "407" in first_line:
            log("FritzBox fordert Digest-Authentifizierung – berechne…", ok=True)

            # Parse WWW-Authenticate / Proxy-Authenticate
            realm = nonce = algo = ""
            for line in resp1.split("\r\n"):
                ll = line.lower()
                if ll.startswith("www-authenticate") or ll.startswith("proxy-authenticate"):
                    for part in line.split(","):
                        p = part.strip()
                        k = p.lower()
                        if "realm=" in k:
                            realm = p.split("=", 1)[1].strip().strip('"')
                        elif "nonce=" in k:
                            nonce = p.split("=", 1)[1].strip().strip('"')
                        elif "algorithm=" in k:
                            algo = p.split("=", 1)[1].strip().strip('"').upper()

            if not nonce:
                log(
                    "Konnte nonce nicht aus 401-Antwort lesen – "
                    "FritzBox sendet unbekanntes Auth-Format.",
                    ok=False,
                )
                return {"success": False, "steps": steps}

            log(f"realm={realm!r}  algo={algo or 'MD5'}")

            uri = f"sip:{host}"
            ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()
            resp_digest = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()

            auth_header = (
                f'Digest username="{username}",realm="{realm}",'
                f'nonce="{nonce}",uri="{uri}",'
                f'response="{resp_digest}",algorithm=MD5'
            )
            ah_name = "Authorization" if "401" in first_line else "Proxy-Authorization"

            branch2 = "z9hG4bK" + uuid.uuid4().hex[:16]
            register2 = "\r\n".join([
                f"REGISTER sip:{host} SIP/2.0",
                f"Via: SIP/2.0/UDP {my_ip}:{my_port};branch={branch2};rport",
                f'From: "{username}" <sip:{username}@{host}>;tag={tag}',
                f'To: "{username}" <sip:{username}@{host}>',
                f"Call-ID: {call_id}",
                "CSeq: 2 REGISTER",
                f"Contact: <sip:{username}@{my_ip}:{my_port}>",
                "Max-Forwards: 70",
                "Expires: 300",
                f"{ah_name}: {auth_header}",
                "Content-Length: 0",
                "\r\n",
            ])

            log("Sende REGISTER mit Digest-Authentifizierung…")
            sock.sendto(register2.encode(), (host, port))

            ready = _select.select([sock], [], [], timeout)
            if not ready[0]:
                log(
                    "Keine Antwort auf authentifiziertes REGISTER – "
                    "Timeout nach Authentifizierung.",
                    ok=False,
                )
                return {"success": False, "steps": steps}

            data2, addr2 = sock.recvfrom(4096)
            resp2 = data2.decode("utf-8", errors="replace")
            first_line2 = resp2.split("\r\n")[0]
            success = "200" in first_line2

            if success:
                log(f"Antwort: {first_line2} – Registrierung erfolgreich!", ok=True)
                log("Zugangsdaten korrekt. Verbindung zur FritzBox möglich.", ok=True)
            elif "401" in first_line2 or "403" in first_line2:
                log(
                    f"Antwort: {first_line2} – "
                    "Falscher Benutzername oder Passwort.",
                    ok=False,
                )
            else:
                log(f"Antwort: {first_line2}", ok=None)

            return {"success": success, "steps": steps}

        elif "200" in first_line:
            log("Registrierung ohne Authentifizierung akzeptiert.", ok=True)
            return {"success": True, "steps": steps}

        elif "403" in first_line:
            log(
                "403 Forbidden – FritzBox verweigert Zugriff. "
                "Gerät in FritzBox angelegt und richtige Rufnummer zugewiesen?",
                ok=False,
            )
        elif "404" in first_line:
            log("404 Not Found – Benutzername unbekannt.", ok=False)
        elif "400" in first_line:
            log(
                "400 Bad Request – FritzBox lehnt das SIP-Paket ab. "
                "Bitte Benutzernamen prüfen.",
                ok=False,
            )
        else:
            log(f"Unerwartete Antwort: {first_line}", ok=None)

        return {"success": False, "steps": steps}

    except OSError as e:
        log(f"Netzwerkfehler: {e}", ok=False)
        return {"success": False, "steps": steps}
    finally:
        sock.close()

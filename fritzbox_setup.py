"""
FritzBox auto-discovery and automated SIP device setup via TR-064 SOAP API.

Discovery methods:
  1. fritz.box hostname resolution
  2. SSDP multicast (UPnP M-SEARCH)
  3. Default gateway IP
  4. Common FritzBox IPs (192.168.178.1, 192.168.0.1, 192.168.1.1)

Setup:
  - Reads configured internal IP-phones via X_VoIP:1 TR-064 service
  - Optionally sets a new SIP password via X_AVM-DE_SetClient
  - Falls back to manual instructions if automatic password setting fails
"""

import logging
import random
import socket
import string
import threading
import time

import requests
from requests.auth import HTTPDigestAuth
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

TR064_PORT = 49000
TR064_VOIP_PATH = "/upnp/control/x_voip"
TR064_VOIP_NS = "urn:dslforum-org:service:X_VoIP:1"


# ── SOAP helper ───────────────────────────────────────────────────────────────

def _soap(ip: str, password: str, action: str, params: dict = None,
          username: str = "admin", timeout: int = 10) -> ET.Element:
    """Make a TR-064 SOAP call. Returns the parsed body element."""
    params = params or {}
    params_xml = "".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{TR064_VOIP_NS}">'
        f'{params_xml}'
        f'</u:{action}></s:Body></s:Envelope>'
    )
    url = f"http://{ip}:{TR064_PORT}{TR064_VOIP_PATH}"
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{TR064_VOIP_NS}#{action}"',
    }
    resp = requests.post(url, data=body.encode(), headers=headers,
                         auth=HTTPDigestAuth(username, password), timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    # Find the Body element regardless of namespace prefix
    body_el = (root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Body")
               or root.find(".//Body")
               or root)
    return body_el


def _soap_text(body_el: ET.Element, local_name: str) -> str:
    """Extract text from a SOAP response element by local name (ignoring namespace)."""
    for el in body_el.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == local_name:
            return el.text or ""
    return ""


# ── FritzBox probing ──────────────────────────────────────────────────────────

def _probe(ip: str, timeout: float = 3.0) -> dict | None:
    """
    Test if ip is a FritzBox by querying the TR-064 descriptor.
    Returns {"ip", "friendly_name", "model"} or None.
    """
    try:
        resp = requests.get(f"http://{ip}:{TR064_PORT}/tr64desc.xml",
                            timeout=timeout)
        text = resp.text
        if resp.status_code == 200 and ("FRITZ" in text.upper() or "AVM" in text.upper()):
            root = ET.fromstring(resp.content)
            # Try to parse friendly name and model
            friendly = "FRITZ!Box"
            model = ""
            for el in root.iter():
                tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                if tag == "friendlyName" and el.text:
                    friendly = el.text.strip()
                if tag == "modelName" and el.text:
                    model = el.text.strip()
            return {"ip": ip, "friendly_name": friendly, "model": model}
    except Exception:
        pass

    # Fallback: try HTTP port 80 and look for FritzBox in response
    try:
        resp = requests.get(f"http://{ip}/", timeout=timeout, allow_redirects=True)
        combined = (resp.text[:800] + str(resp.headers)).lower()
        if "fritz" in combined or ("avm" in combined and "box" in combined):
            return {"ip": ip, "friendly_name": "FRITZ!Box", "model": ""}
    except Exception:
        pass

    return None


def _ssdp_search(timeout: float = 3.0) -> list[str]:
    """Send UPnP SSDP M-SEARCH and return IPs that look like FritzBox."""
    ips: list[str] = []
    MCAST = ("239.255.255.250", 1900)
    search_types = [
        "urn:dslforum-org:device:InternetGatewayDevice:1",
        "upnp:rootdevice",
    ]
    for st in search_types:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {MCAST[0]}:{MCAST[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            f"ST: {st}\r\n\r\n"
        ).encode()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.settimeout(timeout)
            sock.sendto(msg, MCAST)
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                    ip = addr[0]
                    text = data.decode("utf-8", errors="ignore").lower()
                    if ("fritz" in text or "avm" in text) and ip not in ips:
                        ips.append(ip)
                except socket.timeout:
                    break
            sock.close()
        except Exception as e:
            logger.debug(f"SSDP error: {e}")
    return ips


def _default_gateway() -> str | None:
    """Return the default gateway IP (usually the router)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            parts = s.getsockname()[0].split(".")
        return f"{parts[0]}.{parts[1]}.{parts[2]}.1"
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def discover_fritzbox(timeout: float = 4.0) -> list[dict]:
    """
    Discover FritzBox devices on the local network using multiple methods.
    Returns list of dicts: {"ip", "friendly_name", "model"}.
    """
    candidates: list[str] = []

    # 1. fritz.box hostname
    try:
        ip = socket.gethostbyname("fritz.box")
        if ip not in candidates:
            candidates.append(ip)
    except Exception:
        pass

    # 2. SSDP
    for ip in _ssdp_search(timeout=min(timeout, 3.0)):
        if ip not in candidates:
            candidates.append(ip)

    # 3. Default gateway
    gw = _default_gateway()
    if gw and gw not in candidates:
        candidates.append(gw)

    # 4. Common FritzBox IPs
    for ip in ["192.168.178.1", "192.168.0.1", "192.168.1.1", "192.168.2.1"]:
        if ip not in candidates:
            candidates.append(ip)

    # Probe all candidates in parallel
    results: list[dict] = []
    lock = threading.Lock()

    def probe(ip: str):
        info = _probe(ip, timeout=3.0)
        if info:
            with lock:
                results.append(info)

    threads = [threading.Thread(target=probe, args=(ip,), daemon=True)
               for ip in candidates]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    # Deduplicate by IP, keep order (fritz.box / SSDP results first)
    seen: set[str] = set()
    unique: list[dict] = []
    for r in results:
        if r["ip"] not in seen:
            seen.add(r["ip"])
            unique.append(r)
    return unique


def get_voip_clients(ip: str, password: str,
                     username: str = "admin") -> tuple[bool, str, list[dict]]:
    """
    Retrieve configured internal SIP phone accounts from FritzBox via TR-064.
    Returns (ok, message, clients).
    Each client dict has keys: index, ClientId, ClientUsername, ClientRegistrar,
    ClientRegistrarPort, ClientDialKey, ClientCallGroup.
    """
    try:
        body = _soap(ip, password, "X_AVM-DE_GetNumberOfClients", username=username)
        count = int(_soap_text(body, "NewX_AVM-DE_NumberOfClients") or "0")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if code == 401:
            return False, "Falsches FritzBox-Passwort (401 Unauthorized)", []
        return False, f"TR-064 HTTP-Fehler {code}: {e}", []
    except requests.exceptions.ConnectionError:
        return False, (f"Keine Verbindung zu {ip}:{TR064_PORT} – "
                       "ist TR-064 in der FritzBox aktiviert? "
                       "(Heimnetz → Netzwerk → Heimnetzfreigaben)"), []
    except Exception as e:
        return False, f"Fehler: {e}", []

    clients: list[dict] = []
    for i in range(count):
        try:
            cb = _soap(ip, password, "X_AVM-DE_GetClient",
                       {"NewX_AVM-DE_ClientIndex": str(i)}, username=username)
            client: dict = {"index": i}
            for el in cb.iter():
                tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                if tag.startswith("NewX_AVM-DE_"):
                    key = tag[len("NewX_AVM-DE_"):]
                    client[key] = el.text or ""
            # Fallback: if ClientId is missing use index
            if "ClientId" not in client:
                client["ClientId"] = str(620 + i)
            clients.append(client)
        except Exception as e:
            logger.debug(f"Could not read VoIP client {i}: {e}")

    return True, f"{count} IP-Telefon(e) konfiguriert", clients


def set_client_password(ip: str, fb_password: str, client_index: int,
                        client_id: str, new_sip_password: str,
                        fb_username: str = "admin") -> tuple[bool, str]:
    """
    Attempt to set the SIP password for an existing internal phone via TR-064.
    Tries several action variants to handle different FritzOS versions.
    Returns (ok, message). If ok=False the caller should fall back to manual setup.
    """
    # Different FritzOS versions use different action/field names
    variants = [
        ("X_AVM-DE_SetClient4", "NewX_AVM-DE_ClientPassword"),
        ("X_AVM-DE_SetClient3", "NewX_AVM-DE_ClientPassword"),
        ("X_AVM-DE_SetClient",  "NewX_AVM-DE_ClientPassword"),
        ("X_AVM-DE_SetClient4", "NewX_AVM-DE_ClientSecret"),
        ("X_AVM-DE_SetClient3", "NewX_AVM-DE_ClientSecret"),
    ]
    last_error = ""
    for action, pw_field in variants:
        try:
            _soap(ip, fb_password, action, {
                "NewX_AVM-DE_ClientIndex": str(client_index),
                "NewX_AVM-DE_ClientId":    client_id,
                pw_field:                  new_sip_password,
            }, username=fb_username)
            return True, f"Passwort für Gerät {client_id} erfolgreich gesetzt"
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 401:
                return False, "Falsches FritzBox-Passwort"
            last_error = f"HTTP {code}"
            # 500 = action unsupported by this FritzOS version → try next variant
        except Exception as e:
            last_error = str(e)

    logger.debug(f"All SetClient variants failed. Last error: {last_error}")
    return False, "auto"   # signal caller to use manual instructions


def generate_password(length: int = 16) -> str:
    """Generate a cryptographically random SIP password."""
    chars = string.ascii_letters + string.digits + "!@#%"
    return "".join(random.SystemRandom().choice(chars) for _ in range(length))

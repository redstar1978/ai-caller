"""
Ollama network discovery and connection testing.
"""

import ipaddress
import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

import requests

logger = logging.getLogger(__name__)

OLLAMA_PORT = 11434
SCAN_TIMEOUT = 0.4   # seconds per host for port check
HTTP_TIMEOUT = 5     # seconds for HTTP calls


def _get_local_subnet() -> str:
    """Return the local subnet in CIDR notation, e.g. '192.168.178.0/24'."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return str(net)
    except Exception:
        return "192.168.178.0/24"


def _port_open(host: str, port: int, timeout: float = SCAN_TIMEOUT) -> bool:
    """Return True if the TCP port is open on host."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _fetch_ollama_info(base_url: str) -> dict | None:
    """
    Try to reach Ollama at base_url.
    Returns dict with url, version, models on success, None on failure.
    """
    try:
        # GET /api/version
        r = requests.get(f"{base_url}/api/version", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        version = r.json().get("version", "?")

        # GET /api/tags – list installed models
        r2 = requests.get(f"{base_url}/api/tags", timeout=HTTP_TIMEOUT)
        r2.raise_for_status()
        models = [m["name"] for m in r2.json().get("models", [])]

        return {"url": base_url, "version": version, "models": models}
    except Exception:
        return None


def scan_network(extra_hosts: List[str] = None) -> List[dict]:
    """
    Scan the local /24 subnet for Ollama instances on port 11434.
    Also checks localhost and any extra_hosts provided.

    Returns list of dicts: [{url, version, models}, …]
    """
    subnet = _get_local_subnet()
    hosts = ["127.0.0.1", "localhost"]

    try:
        network = ipaddress.IPv4Network(subnet)
        hosts += [str(ip) for ip in network.hosts()]
    except Exception as e:
        logger.warning(f"Subnet scan error: {e}")

    if extra_hosts:
        hosts += extra_hosts

    # Remove duplicates while preserving order
    seen = set()
    unique_hosts = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            unique_hosts.append(h)

    # Phase 1: parallel TCP port scan
    open_hosts = []
    with ThreadPoolExecutor(max_workers=80) as ex:
        futures = {ex.submit(_port_open, h, OLLAMA_PORT): h for h in unique_hosts}
        for future in as_completed(futures):
            h = futures[future]
            try:
                if future.result():
                    open_hosts.append(h)
            except Exception:
                pass

    # Phase 2: HTTP probe on open hosts
    found = []
    for host in open_hosts:
        url = f"http://{host}:{OLLAMA_PORT}"
        info = _fetch_ollama_info(url)
        if info:
            found.append(info)
            logger.info(f"Ollama found at {url} v{info['version']} – {len(info['models'])} models")

    return found


def test_connection(base_url: str, model: str) -> dict:
    """
    Test an Ollama connection and specific model.
    Returns {ok, url, version, models, model_available, model_tested, error}.
    """
    base_url = base_url.rstrip("/")
    result = {
        "ok": False,
        "url": base_url,
        "version": None,
        "models": [],
        "model_available": False,
        "model_tested": model,
        "error": None,
    }

    # 1. Reach the API
    try:
        r = requests.get(f"{base_url}/api/version", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        result["version"] = r.json().get("version", "?")
    except requests.exceptions.ConnectionError:
        result["error"] = f"Verbindung zu {base_url} fehlgeschlagen – ist Ollama gestartet?"
        return result
    except requests.exceptions.Timeout:
        result["error"] = f"Zeitüberschreitung beim Verbinden mit {base_url} (>{HTTP_TIMEOUT}s)"
        return result
    except requests.exceptions.HTTPError as e:
        result["error"] = f"HTTP-Fehler: {e}"
        return result
    except Exception as e:
        result["error"] = f"Unbekannter Fehler: {e}"
        return result

    # 2. List models
    try:
        r2 = requests.get(f"{base_url}/api/tags", timeout=HTTP_TIMEOUT)
        r2.raise_for_status()
        result["models"] = [m["name"] for m in r2.json().get("models", [])]
    except Exception as e:
        result["error"] = f"Modell-Liste konnte nicht abgerufen werden: {e}"
        return result

    # 3. Check if the requested model is available
    model_base = model.split(":")[0]
    result["model_available"] = any(
        m == model or m.startswith(model_base) for m in result["models"]
    )

    if not result["models"]:
        result["error"] = "Ollama erreichbar, aber keine Modelle installiert. Führe z.B. 'ollama pull llama3' aus."
        return result

    if not result["model_available"]:
        available = ", ".join(result["models"][:10])
        result["error"] = (
            f"Modell '{model}' nicht gefunden. "
            f"Installierte Modelle: {available}. "
            f"Installieren mit: ollama pull {model}"
        )
        return result

    # 4. Quick functional test – send a minimal chat request
    try:
        r3 = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Antworte nur mit: OK"}],
                "stream": False,
                "options": {"num_predict": 5},
            },
            timeout=30,
        )
        r3.raise_for_status()
        reply = r3.json().get("message", {}).get("content", "").strip()
        result["ok"] = True
        result["test_reply"] = reply[:100]
    except requests.exceptions.Timeout:
        result["error"] = (
            f"Modell '{model}' antwortet nicht innerhalb von 30s. "
            "Möglicherweise wird es gerade geladen – kurz warten und erneut testen."
        )
        return result
    except Exception as e:
        result["error"] = f"Funktionstest fehlgeschlagen: {e}"
        return result

    return result


def test_cloud_connection(api_key: str, base_url: str, model: str) -> dict:
    """Test an OpenAI-compatible cloud LLM endpoint."""
    result = {"ok": False, "model_tested": model, "error": None, "test_reply": None}
    if not api_key:
        result["error"] = "Kein API-Key angegeben."
        return result
    try:
        import openai
        client = openai.OpenAI(
            api_key=api_key,
            base_url=(base_url.rstrip("/") if base_url else "https://api.openai.com/v1"),
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Antworte nur mit: OK"}],
            max_tokens=5,
        )
        result["test_reply"] = resp.choices[0].message.content.strip()[:100]
        result["ok"] = True
    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err or "invalid_api_key" in err:
            result["error"] = "Ungültiger API-Key. Bitte prüfen."
        elif "404" in err or "model" in err.lower():
            result["error"] = f"Modell '{model}' nicht gefunden. Bitte Modellname prüfen."
        elif "connection" in err.lower() or "connect" in err.lower():
            result["error"] = f"Verbindung zur API-URL fehlgeschlagen: {base_url or 'api.openai.com'}"
        else:
            result["error"] = f"API-Fehler: {err[:200]}"
    return result

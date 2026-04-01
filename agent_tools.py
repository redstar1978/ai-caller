"""
Agent Tools – web search, Google Calendar, Nextcloud CalDAV.

Tool definitions are returned as OpenAI-compatible function/tool specs.
execute_tool() dispatches by name and returns a result string for the LLM.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)


# ── Tool definitions ───────────────────────────────────────────────────────────

def get_tool_definitions(cfg: dict) -> list:
    """Return list of enabled OpenAI-format tool dicts based on config."""
    tools = []

    if cfg.get("agent_web_search_enabled") == "true" and cfg.get("agent_brave_api_key"):
        tools.append({
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Suche im Internet nach aktuellen Informationen.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Suchanfrage"},
                        "count": {"type": "integer", "description": "Anzahl Ergebnisse (1-10)", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        })

    if cfg.get("agent_calendar_enabled") == "true":
        provider = cfg.get("agent_calendar_provider", "none")
        mode = cfg.get("agent_calendar_mode", "readonly")

        if provider in ("google", "nextcloud"):
            tools.append({
                "type": "function",
                "function": {
                    "name": "get_calendar_events",
                    "description": "Lese Kalendereinträge aus dem Kalender.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "days_ahead": {
                                "type": "integer",
                                "description": "Wie viele Tage in die Zukunft schauen (Standard: 7)",
                                "default": 7,
                            },
                            "days_back": {
                                "type": "integer",
                                "description": "Wie viele Tage in die Vergangenheit schauen (Standard: 0)",
                                "default": 0,
                            },
                        },
                        "required": [],
                    },
                },
            })

        if provider in ("google", "nextcloud") and mode == "full":
            tools.append({
                "type": "function",
                "function": {
                    "name": "create_calendar_event",
                    "description": "Erstelle einen neuen Kalendereintrag.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Titel des Termins"},
                            "start": {"type": "string", "description": "Startzeit als ISO-8601 (z.B. 2025-03-15T14:00:00)"},
                            "end": {"type": "string", "description": "Endzeit als ISO-8601 (z.B. 2025-03-15T15:00:00)"},
                            "description": {"type": "string", "description": "Beschreibung (optional)"},
                            "location": {"type": "string", "description": "Ort (optional)"},
                        },
                        "required": ["title", "start", "end"],
                    },
                },
            })
            tools.append({
                "type": "function",
                "function": {
                    "name": "delete_calendar_event",
                    "description": "Lösche einen Kalendereintrag anhand seines Titels oder UIDs.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Titel des zu löschenden Termins"},
                        },
                        "required": ["title"],
                    },
                },
            })

    return tools


# ── Tool executor ──────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict, cfg: dict) -> str:
    """Execute a tool by name and return result as string."""
    try:
        if name == "search_web":
            return _search_web(args.get("query", ""), int(args.get("count", 5)), cfg)
        elif name == "get_calendar_events":
            return _get_calendar_events(int(args.get("days_ahead", 7)), int(args.get("days_back", 0)), cfg)
        elif name == "create_calendar_event":
            return _create_calendar_event(
                args.get("title", ""),
                args.get("start", ""),
                args.get("end", ""),
                args.get("description", ""),
                args.get("location", ""),
                cfg,
            )
        elif name == "delete_calendar_event":
            return _delete_calendar_event(args.get("title", ""), cfg)
        else:
            return f"Unbekanntes Tool: {name}"
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return f"Fehler bei {name}: {e}"


# ── Web search ─────────────────────────────────────────────────────────────────

def _search_web(query: str, count: int, cfg: dict) -> str:
    api_key = cfg.get("agent_brave_api_key", "")
    if not api_key:
        return "Kein Brave API-Key konfiguriert."
    count = max(1, min(count, 10))
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": count, "search_lang": "de"},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("web", {}).get("results", [])
    if not results:
        return "Keine Suchergebnisse gefunden."
    lines = [f"Suchergebnisse für: {query}"]
    for r in results:
        title = r.get("title", "")
        url = r.get("url", "")
        desc = r.get("description", "")
        lines.append(f"\n• {title}\n  {url}\n  {desc}")
    return "\n".join(lines)


# ── Google Calendar ────────────────────────────────────────────────────────────

def _google_access_token(cfg: dict) -> str:
    """Exchange refresh token for a short-lived access token."""
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": cfg.get("agent_google_client_id", ""),
            "client_secret": cfg.get("agent_google_client_secret", ""),
            "refresh_token": cfg.get("agent_google_refresh_token", ""),
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get_google_events(days_ahead: int, days_back: int, cfg: dict) -> str:
    token = _google_access_token(cfg)
    cal_id = cfg.get("agent_google_calendar_id", "primary")
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_back)).isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()
    resp = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(cal_id, safe='')}/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 20,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        return "Keine Termine im angegebenen Zeitraum."
    lines = [f"Kalendereinträge ({days_back} Tage zurück – {days_ahead} Tage voraus):"]
    for item in items:
        summary = item.get("summary", "(Kein Titel)")
        start = item.get("start", {})
        start_str = start.get("dateTime", start.get("date", ""))
        location = item.get("location", "")
        desc = item.get("description", "")
        line = f"\n• {start_str}: {summary}"
        if location:
            line += f"\n  Ort: {location}"
        if desc:
            line += f"\n  {desc[:100]}"
        lines.append(line)
    return "\n".join(lines)


def _create_google_event(title: str, start: str, end: str,
                         description: str, location: str, cfg: dict) -> str:
    token = _google_access_token(cfg)
    cal_id = cfg.get("agent_google_calendar_id", "primary")
    body = {
        "summary": title,
        "start": {"dateTime": start, "timeZone": "Europe/Berlin"},
        "end": {"dateTime": end, "timeZone": "Europe/Berlin"},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    resp = requests.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(cal_id, safe='')}/events",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return f"Termin '{title}' am {start} wurde erstellt."


def _delete_google_event(title: str, cfg: dict) -> str:
    token = _google_access_token(cfg)
    cal_id = cfg.get("agent_google_calendar_id", "primary")
    now = datetime.now(timezone.utc)
    resp = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(cal_id, safe='')}/events",
        params={"q": title, "timeMin": now.isoformat(), "singleEvents": "true", "maxResults": 5},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    matches = [i for i in items if title.lower() in i.get("summary", "").lower()]
    if not matches:
        return f"Kein Termin mit dem Titel '{title}' gefunden."
    event_id = matches[0]["id"]
    del_resp = requests.delete(
        f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(cal_id, safe='')}/events/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    del_resp.raise_for_status()
    return f"Termin '{matches[0].get('summary', title)}' wurde gelöscht."


# ── Nextcloud CalDAV ───────────────────────────────────────────────────────────

def _nc_base_url(cfg: dict) -> str:
    url = cfg.get("agent_nextcloud_url", "").rstrip("/")
    user = cfg.get("agent_nextcloud_username", "")
    cal = cfg.get("agent_nextcloud_calendar_name", "personal")
    return f"{url}/remote.php/dav/calendars/{user}/{cal}"


def _nc_auth(cfg: dict):
    return (cfg.get("agent_nextcloud_username", ""), cfg.get("agent_nextcloud_app_password", ""))


def _parse_icalendar(ical_text: str) -> list:
    """Minimal VEVENT parser — returns list of dicts."""
    events = []
    for vevent in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ical_text, re.DOTALL):
        e = {}
        for line in vevent.splitlines():
            if line.startswith("DTSTART"):
                e["start"] = line.split(":", 1)[-1].strip()
            elif line.startswith("DTEND"):
                e["end"] = line.split(":", 1)[-1].strip()
            elif line.startswith("SUMMARY"):
                e["summary"] = line.split(":", 1)[-1].strip()
            elif line.startswith("DESCRIPTION"):
                e["description"] = line.split(":", 1)[-1].strip()
            elif line.startswith("LOCATION"):
                e["location"] = line.split(":", 1)[-1].strip()
            elif line.startswith("UID"):
                e["uid"] = line.split(":", 1)[-1].strip()
        if e.get("summary"):
            events.append(e)
    return events


def _format_nc_date(dtstr: str) -> str:
    """Convert CalDAV date string to readable format."""
    dtstr = dtstr.replace("Z", "").replace("T", " ")
    return dtstr[:16] if len(dtstr) >= 16 else dtstr


def _get_nextcloud_events(days_ahead: int, days_back: int, cfg: dict) -> str:
    base = _nc_base_url(cfg)
    auth = _nc_auth(cfg)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(days=days_ahead)).strftime("%Y%m%dT%H%M%SZ")
    body = f"""<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""
    resp = requests.request(
        "REPORT", base,
        data=body.encode("utf-8"),
        auth=auth,
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    events = _parse_icalendar(resp.text)
    if not events:
        return "Keine Termine im angegebenen Zeitraum."
    lines = [f"Kalendereinträge ({days_back} Tage zurück – {days_ahead} Tage voraus):"]
    for e in sorted(events, key=lambda x: x.get("start", "")):
        start_str = _format_nc_date(e.get("start", ""))
        line = f"\n• {start_str}: {e['summary']}"
        if e.get("location"):
            line += f"\n  Ort: {e['location']}"
        if e.get("description"):
            line += f"\n  {e['description'][:100]}"
        lines.append(line)
    return "\n".join(lines)


def _create_nextcloud_event(title: str, start: str, end: str,
                            description: str, location: str, cfg: dict) -> str:
    base = _nc_base_url(cfg)
    auth = _nc_auth(cfg)
    uid = str(uuid.uuid4())
    # Convert ISO 8601 to iCal format: 2025-03-15T14:00:00 → 20250315T140000
    def to_ical_dt(dt: str) -> str:
        return dt.replace("-", "").replace(":", "").replace(" ", "T").split("+")[0].split("Z")[0]

    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ical = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ai-caller//agent//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{to_ical_dt(start)}",
        f"DTEND:{to_ical_dt(end)}",
        f"SUMMARY:{title}",
    ]
    if description:
        ical.append(f"DESCRIPTION:{description}")
    if location:
        ical.append(f"LOCATION:{location}")

    ical += ["END:VEVENT", "END:VCALENDAR"]
    ical_data = "\r\n".join(ical).encode("utf-8")

    resp = requests.put(
        f"{base}/{uid}.ics",
        data=ical_data,
        auth=auth,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        timeout=15,
    )
    resp.raise_for_status()
    return f"Termin '{title}' am {start} wurde erstellt."


def _delete_nextcloud_event(title: str, cfg: dict) -> str:
    """Find event by title via REPORT, then DELETE it."""
    base = _nc_base_url(cfg)
    auth = _nc_auth(cfg)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(days=365)).strftime("%Y%m%dT%H%M%SZ")
    body = f"""<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/><D:href/><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""
    resp = requests.request(
        "REPORT", base,
        data=body.encode("utf-8"),
        auth=auth,
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        timeout=15,
    )
    resp.raise_for_status()

    # Extract hrefs and their summaries
    hrefs = re.findall(r"<D:href>(.*?)</D:href>", resp.text)
    events = _parse_icalendar(resp.text)

    match = next((e for e in events if title.lower() in e.get("summary", "").lower()), None)
    if not match:
        return f"Kein Termin mit dem Titel '{title}' gefunden."

    uid = match.get("uid", "")
    href = next((h for h in hrefs if uid in h or title.lower().replace(" ", "-") in h.lower()), None)
    if not href:
        # Try to construct URL from UID
        nc_url = cfg.get("agent_nextcloud_url", "").rstrip("/")
        href = f"{_nc_base_url(cfg)}/{uid}.ics".replace(nc_url, "")

    nc_url = cfg.get("agent_nextcloud_url", "").rstrip("/")
    del_url = nc_url + href if href.startswith("/") else href
    del_resp = requests.delete(del_url, auth=auth, timeout=15)
    del_resp.raise_for_status()
    return f"Termin '{match['summary']}' wurde gelöscht."


# ── Dispatcher helpers ─────────────────────────────────────────────────────────

def _get_calendar_events(days_ahead: int, days_back: int, cfg: dict) -> str:
    provider = cfg.get("agent_calendar_provider", "none")
    if provider == "google":
        return _get_google_events(days_ahead, days_back, cfg)
    elif provider == "nextcloud":
        return _get_nextcloud_events(days_ahead, days_back, cfg)
    return "Kein Kalender-Anbieter konfiguriert."


def _create_calendar_event(title: str, start: str, end: str,
                           description: str, location: str, cfg: dict) -> str:
    provider = cfg.get("agent_calendar_provider", "none")
    if provider == "google":
        return _create_google_event(title, start, end, description, location, cfg)
    elif provider == "nextcloud":
        return _create_nextcloud_event(title, start, end, description, location, cfg)
    return "Kein Kalender-Anbieter konfiguriert."


def _delete_calendar_event(title: str, cfg: dict) -> str:
    provider = cfg.get("agent_calendar_provider", "none")
    if provider == "google":
        return _delete_google_event(title, cfg)
    elif provider == "nextcloud":
        return _delete_nextcloud_event(title, cfg)
    return "Kein Kalender-Anbieter konfiguriert."

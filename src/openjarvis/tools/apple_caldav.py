"""Apple iCloud CalDAV tools — events + reminders (VTODO).

Authenticates to iCloud's CalDAV endpoint using an Apple ID + an
app-specific password. Discovery follows the standard CalDAV flow:

    base → current-user-principal → calendar-home-set → list calendars

Calendars whose ``supported-calendar-component-set`` contains ``VTODO``
are surfaced as reminder lists; calendars with ``VEVENT`` are surfaced
as event calendars. The first event-capable and todo-capable calendars
become the defaults unless ``APPLE_DEFAULT_CALENDAR_NAME`` /
``APPLE_DEFAULT_REMINDER_NAME`` are set.

Credentials come from env vars:
    APPLE_ID_EMAIL      — Apple ID email
    APPLE_APP_PASSWORD  — app-specific password (xxxx-xxxx-xxxx-xxxx)

Discovery results are cached in-process; restart the container to
re-discover after creating a new calendar.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

ICLOUD_BASE = "https://caldav.icloud.com"
NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "ic": "http://apple.com/ns/ical/",
}


def _creds() -> Tuple[str, str]:
    user = os.environ.get("APPLE_ID_EMAIL", "").strip()
    pwd = os.environ.get("APPLE_APP_PASSWORD", "").strip()
    if not user or not pwd:
        raise RuntimeError(
            "APPLE_ID_EMAIL and APPLE_APP_PASSWORD must be set in the env."
        )
    return user, pwd


def _basic_auth_header() -> str:
    user, pwd = _creds()
    raw = f"{user}:{pwd}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't auto-follow redirects on PROPFIND/REPORT — we need to read
    the Location header manually because urllib downgrades the method."""

    def redirect_request(self, *a, **k):  # type: ignore[override]
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _request(
    method: str,
    url: str,
    *,
    body: bytes = b"",
    headers: Optional[Dict[str, str]] = None,
    follow_redirects: bool = True,
    max_hops: int = 5,
) -> Tuple[int, Dict[str, str], bytes]:
    """Issue a CalDAV request. Returns (status, headers, body)."""

    req_headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/xml; charset=utf-8",
        "User-Agent": "Javris/1.0 CalDAV",
    }
    if headers:
        req_headers.update(headers)

    current_url = url
    for _ in range(max_hops):
        req = urllib.request.Request(current_url, data=body, method=method)
        for k, v in req_headers.items():
            req.add_header(k, v)
        try:
            with _opener.open(req, timeout=30) as resp:
                status = resp.status
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                payload = resp.read()
                return status, response_headers, payload
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 307, 308) and follow_redirects:
                loc = exc.headers.get("Location")
                if not loc:
                    raise
                current_url = urllib.parse.urljoin(current_url, loc)
                continue
            raise
    raise RuntimeError(f"Too many redirects for {url}")


def _propfind(url: str, body: str, depth: str = "0") -> ET.Element:
    status, _, payload = _request(
        "PROPFIND",
        url,
        body=body.encode(),
        headers={"Depth": depth},
    )
    if status >= 400:
        raise RuntimeError(f"PROPFIND {url} → HTTP {status}: {payload[:300]!r}")
    return ET.fromstring(payload)


_PROPFIND_PRINCIPAL = """\
<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal/></d:prop>
</d:propfind>"""

_PROPFIND_HOME = """\
<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>"""

_PROPFIND_CALENDARS = """\
<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://calendarserver.org/ns/">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""


_discovery_cache: Optional[Dict[str, Any]] = None


def _href_text(elem: ET.Element) -> str:
    h = elem.find("d:href", NS)
    return (h.text or "").strip() if h is not None else ""


def _discover() -> Dict[str, Any]:
    global _discovery_cache
    if _discovery_cache is not None:
        return _discovery_cache

    # Step 1 — principal URL
    root = _propfind(ICLOUD_BASE + "/", _PROPFIND_PRINCIPAL)
    principal_elem = root.find(".//d:current-user-principal/d:href", NS)
    if principal_elem is None or not principal_elem.text:
        raise RuntimeError("CalDAV discovery: no current-user-principal returned")
    principal_url = urllib.parse.urljoin(ICLOUD_BASE, principal_elem.text.strip())

    # Step 2 — calendar home
    root = _propfind(principal_url, _PROPFIND_HOME)
    home_elem = root.find(".//c:calendar-home-set/d:href", NS)
    if home_elem is None or not home_elem.text:
        raise RuntimeError("CalDAV discovery: no calendar-home-set returned")
    home_url = urllib.parse.urljoin(principal_url, home_elem.text.strip())

    # Step 3 — list calendars under home
    root = _propfind(home_url, _PROPFIND_CALENDARS, depth="1")
    event_calendars: List[Dict[str, str]] = []
    todo_calendars: List[Dict[str, str]] = []
    for resp in root.findall("d:response", NS):
        href = _href_text(resp)
        if not href:
            continue
        full = urllib.parse.urljoin(home_url, href)
        prop = resp.find("d:propstat/d:prop", NS)
        if prop is None:
            continue
        rtype = prop.find("d:resourcetype", NS)
        if rtype is None or rtype.find("c:calendar", NS) is None:
            continue  # only true calendars
        name_elem = prop.find("d:displayname", NS)
        name = (name_elem.text or "").strip() if name_elem is not None else "Calendar"
        comps = prop.find("c:supported-calendar-component-set", NS)
        comp_names = (
            [c.attrib.get("name", "") for c in comps.findall("c:comp", NS)]
            if comps is not None
            else []
        )
        if "VEVENT" in comp_names:
            event_calendars.append({"name": name, "url": full})
        if "VTODO" in comp_names:
            todo_calendars.append({"name": name, "url": full})

    if not event_calendars:
        raise RuntimeError("CalDAV discovery: no VEVENT calendars found")

    # Pick defaults
    pref_event = os.environ.get("APPLE_DEFAULT_CALENDAR_NAME", "").strip()
    pref_todo = os.environ.get("APPLE_DEFAULT_REMINDER_NAME", "").strip()
    default_event = next(
        (c for c in event_calendars if pref_event and c["name"] == pref_event),
        event_calendars[0],
    )
    default_todo = (
        next(
            (c for c in todo_calendars if pref_todo and c["name"] == pref_todo),
            todo_calendars[0] if todo_calendars else None,
        )
    )

    _discovery_cache = {
        "principal_url": principal_url,
        "home_url": home_url,
        "event_calendars": event_calendars,
        "todo_calendars": todo_calendars,
        "default_event": default_event,
        "default_todo": default_todo,
    }
    logger.info(
        "Apple CalDAV discovered: %d event cals, %d todo cals (defaults: %s / %s)",
        len(event_calendars),
        len(todo_calendars),
        default_event["name"],
        (default_todo["name"] if default_todo else "—"),
    )
    return _discovery_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_utc_basic(dt_iso: str) -> str:
    """Convert ISO 8601 (with tz offset) → UTC basic format YYYYMMDDTHHMMSSZ."""
    s = dt_iso.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _ical_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def _now_utc_basic() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _put_ics(calendar_url: str, uid: str, ics: str) -> str:
    """PUT a new VEVENT/VTODO. Returns the ETag URL."""
    href = urllib.parse.urljoin(
        calendar_url if calendar_url.endswith("/") else calendar_url + "/",
        f"{uid}.ics",
    )
    status, headers, body = _request(
        "PUT",
        href,
        body=ics.encode(),
        headers={
            "Content-Type": "text/calendar; charset=utf-8",
            "If-None-Match": "*",
        },
    )
    if status >= 400:
        raise RuntimeError(f"PUT {href} → HTTP {status}: {body[:300]!r}")
    return href


def _calendar_query(calendar_url: str, comp: str, time_range: Optional[Tuple[str, str]] = None) -> List[str]:
    """REPORT calendar-query → list of href URLs of matching components."""
    if time_range:
        tr = (
            f'<c:time-range start="{time_range[0]}" end="{time_range[1]}"/>'
        )
    else:
        tr = ""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="{comp}">{tr}</c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""
    status, _, payload = _request(
        "REPORT",
        calendar_url,
        body=body.encode(),
        headers={"Depth": "1"},
    )
    if status >= 400:
        raise RuntimeError(f"REPORT → HTTP {status}: {payload[:300]!r}")
    root = ET.fromstring(payload)
    out = []
    for resp in root.findall("d:response", NS):
        cdata = resp.find(".//c:calendar-data", NS)
        if cdata is not None and cdata.text:
            out.append(cdata.text)
    return out


def _parse_ical_field(blob: str, name: str) -> str:
    """Best-effort grab of a property value from an iCalendar string."""
    m = re.search(rf"^{re.escape(name)}(?:;[^:\n]*)?:(.*)$", blob, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip()
    return val.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n")


def _parse_dtstart(blob: str) -> str:
    m = re.search(r"^DTSTART(?:;[^:\n]*)?:(.+)$", blob, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@ToolRegistry.register("apple_calendar_today")
class AppleCalendarTodayTool(BaseTool):
    """Today's events from the user's iCloud Calendar (default calendar)."""

    tool_id = "apple_calendar_today"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="apple_calendar_today",
            description=(
                "Return today's events from the user's iCloud (Apple) Calendar. "
                "Use when the user asks about ביומן אפל / Apple calendar / iCloud."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            d = _discover()
            cal = d["default_event"]
            now = datetime.now(timezone.utc)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            ics_blobs = _calendar_query(
                cal["url"],
                "VEVENT",
                time_range=(
                    start.strftime("%Y%m%dT%H%M%SZ"),
                    end.strftime("%Y%m%dT%H%M%SZ"),
                ),
            )
            if not ics_blobs:
                return ToolResult(
                    tool_name=self.spec.name,
                    content=f"No iCloud events today (calendar: {cal['name']}).",
                    success=True,
                )
            lines: List[str] = []
            for blob in ics_blobs:
                title = _parse_ical_field(blob, "SUMMARY") or "(no title)"
                when = _parse_dtstart(blob)
                location = _parse_ical_field(blob, "LOCATION")
                line = f"• {when} — {title}"
                if location:
                    line += f"\n  📍 {location}"
                lines.append(line)
            return ToolResult(
                tool_name=self.spec.name,
                content=f"iCloud calendar '{cal['name']}':\n" + "\n".join(lines),
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"apple_calendar_today error: {exc}",
                success=False,
            )


@ToolRegistry.register("apple_calendar_create_event")
class AppleCalendarCreateEventTool(BaseTool):
    """Create an event on the user's iCloud Calendar."""

    tool_id = "apple_calendar_create_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="apple_calendar_create_event",
            description=(
                "Create a new event on the user's iCloud (Apple) Calendar. "
                "start/end must be ISO 8601 with timezone offset "
                "(e.g. '2026-04-29T14:00:00+03:00'). When the user says "
                "'iCloud', 'Apple', 'יומן אפל' — use this; for Google use "
                "calendar_create_event."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        title = (params.get("title") or "").strip()
        start = (params.get("start") or "").strip()
        end = (params.get("end") or "").strip()
        if not title or not start or not end:
            return ToolResult(
                tool_name=self.spec.name,
                content="title, start, end required.",
                success=False,
            )
        try:
            d = _discover()
            cal = d["default_event"]
            uid = uuid.uuid4().hex + "@javris"
            dtstart = _to_utc_basic(start)
            dtend = _to_utc_basic(end)
            now = _now_utc_basic()
            lines = [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Javris//OpenJarvis//EN",
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                f"SUMMARY:{_ical_escape(title)}",
            ]
            if params.get("description"):
                lines.append(f"DESCRIPTION:{_ical_escape(params['description'])}")
            if params.get("location"):
                lines.append(f"LOCATION:{_ical_escape(params['location'])}")
            lines += ["END:VEVENT", "END:VCALENDAR"]
            ics = "\r\n".join(lines) + "\r\n"
            href = _put_ics(cal["url"], uid, ics)
            return ToolResult(
                tool_name=self.spec.name,
                content=(
                    f"iCloud event created: {title}\n"
                    f"  When: {start} → {end}\n"
                    f"  Calendar: {cal['name']}\n"
                    f"  UID: {uid}"
                ),
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"apple_calendar_create_event error: {exc}",
                success=False,
            )


@ToolRegistry.register("apple_reminders_list")
class AppleRemindersListTool(BaseTool):
    """List open reminders (VTODO) from the user's iCloud Reminders."""

    tool_id = "apple_reminders_list"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="apple_reminders_list",
            description=(
                "Return open Apple iCloud Reminders (VTODO items) from the "
                "default reminders list. Use when the user asks about "
                "תזכורות באפל / Apple Reminders."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            d = _discover()
            cal = d.get("default_todo")
            if not cal:
                return ToolResult(
                    tool_name=self.spec.name,
                    content="No Apple reminders list found on this account.",
                    success=False,
                )
            blobs = _calendar_query(cal["url"], "VTODO")
            open_items: List[str] = []
            for blob in blobs:
                if "STATUS:COMPLETED" in blob:
                    continue
                title = _parse_ical_field(blob, "SUMMARY") or "(no title)"
                due = _parse_ical_field(blob, "DUE")
                line = f"• {title}"
                if due:
                    line += f"  (due {due})"
                open_items.append(line)
            if not open_items:
                return ToolResult(
                    tool_name=self.spec.name,
                    content=f"No open reminders in '{cal['name']}'.",
                    success=True,
                )
            return ToolResult(
                tool_name=self.spec.name,
                content=f"Apple reminders in '{cal['name']}':\n"
                + "\n".join(open_items),
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"apple_reminders_list error: {exc}",
                success=False,
            )


@ToolRegistry.register("apple_reminders_create")
class AppleRemindersCreateTool(BaseTool):
    """Create a new Apple iCloud reminder."""

    tool_id = "apple_reminders_create"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="apple_reminders_create",
            description=(
                "Create a new Apple iCloud reminder (VTODO). Optional 'due' "
                "is ISO 8601 with timezone (will be converted to UTC)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["title"],
            },
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        title = (params.get("title") or "").strip()
        if not title:
            return ToolResult(
                tool_name=self.spec.name,
                content="title required.",
                success=False,
            )
        try:
            d = _discover()
            cal = d.get("default_todo")
            if not cal:
                return ToolResult(
                    tool_name=self.spec.name,
                    content="No reminders list configured on the iCloud account.",
                    success=False,
                )
            uid = uuid.uuid4().hex + "@javris"
            now = _now_utc_basic()
            lines = [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Javris//OpenJarvis//EN",
                "BEGIN:VTODO",
                f"UID:{uid}",
                f"DTSTAMP:{now}",
                f"CREATED:{now}",
                f"SUMMARY:{_ical_escape(title)}",
                "STATUS:NEEDS-ACTION",
            ]
            if params.get("notes"):
                lines.append(f"DESCRIPTION:{_ical_escape(params['notes'])}")
            if params.get("due"):
                lines.append(f"DUE:{_to_utc_basic(params['due'])}")
            lines += ["END:VTODO", "END:VCALENDAR"]
            ics = "\r\n".join(lines) + "\r\n"
            href = _put_ics(cal["url"], uid, ics)
            return ToolResult(
                tool_name=self.spec.name,
                content=(
                    f"Apple reminder created: {title}\n"
                    f"  List: {cal['name']}\n  UID: {uid}"
                ),
                success=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"apple_reminders_create error: {exc}",
                success=False,
            )


__all__ = [
    "AppleCalendarTodayTool",
    "AppleCalendarCreateEventTool",
    "AppleRemindersListTool",
    "AppleRemindersCreateTool",
]

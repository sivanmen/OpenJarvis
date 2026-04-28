"""Live Google tools — direct Gmail / Calendar / Drive queries via REST APIs.

The upstream connectors expose ``mcp_tools()`` ToolSpecs but never wire
implementations.  This module provides concrete BaseTool wrappers so a
ReAct/operative agent can answer questions like "what's in my calendar
today?" or "summarise unread emails" in real time.

Tokens are read from the shared OAuth credential file populated by the
container's entrypoint (``/data/connectors/google.json`` by default).
The access token is refreshed lazily on 401 responses.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_DEFAULT_CREDS = Path(
    os.environ.get(
        "GOOGLE_CREDENTIALS_PATH",
        "/data/connectors/google.json",
    )
)


def _load_tokens() -> Optional[Dict[str, Any]]:
    if not _DEFAULT_CREDS.exists():
        return None
    try:
        return json.loads(_DEFAULT_CREDS.read_text())
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to read %s: %s", _DEFAULT_CREDS, exc)
        return None


def _save_tokens(tokens: Dict[str, Any]) -> None:
    _DEFAULT_CREDS.parent.mkdir(parents=True, exist_ok=True)
    _DEFAULT_CREDS.write_text(json.dumps(tokens, indent=2))
    try:
        os.chmod(_DEFAULT_CREDS, 0o600)
    except OSError:
        pass
    # Mirror to per-connector files used by the connector classes
    for name in ("gdrive", "gcalendar", "gcontacts", "gmail", "google_tasks"):
        path = _DEFAULT_CREDS.parent / f"{name}.json"
        try:
            path.write_text(json.dumps(tokens, indent=2))
            os.chmod(path, 0o600)
        except OSError:
            pass


def _refresh_access_token(tokens: Dict[str, Any]) -> Dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "client_id": tokens["client_id"],
            "client_secret": tokens["client_secret"],
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=payload, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        new_tokens = json.loads(resp.read())
    tokens["access_token"] = new_tokens.get("access_token", tokens.get("access_token", ""))
    tokens["expires_in"] = new_tokens.get("expires_in", 3600)
    tokens["expires_at"] = int(time.time()) + int(tokens.get("expires_in") or 3600) - 60
    _save_tokens(tokens)
    return tokens


def _api_get(url: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tokens = _load_tokens()
    if not tokens or not tokens.get("refresh_token"):
        raise RuntimeError(
            "Google OAuth tokens not configured. Run the OAuth flow and place "
            f"the JSON at {_DEFAULT_CREDS}."
        )

    if int(tokens.get("expires_at") or 0) < int(time.time()):
        tokens = _refresh_access_token(tokens)

    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
            params, doseq=True
        )

    def _do_request(access_token: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    try:
        return _do_request(tokens["access_token"])
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        tokens = _refresh_access_token(tokens)
        return _do_request(tokens["access_token"])


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def _format_event(evt: Dict[str, Any]) -> str:
    summary = evt.get("summary", "(no title)")
    start = evt.get("start", {})
    end = evt.get("end", {})
    when = start.get("dateTime") or start.get("date") or "?"
    location = evt.get("location")
    attendees = evt.get("attendees") or []
    parts = [f"• {when} — {summary}"]
    if location:
        parts.append(f"  📍 {location}")
    if attendees:
        names = ", ".join(
            (a.get("displayName") or a.get("email", "?")) for a in attendees[:5]
        )
        parts.append(f"  👥 {names}")
    return "\n".join(parts)


@ToolRegistry.register("calendar_today")
class CalendarTodayTool(BaseTool):
    """Return today's Google Calendar events on the user's primary calendar."""

    tool_id = "calendar_today"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_today",
            description=(
                "Return all events on the user's primary Google Calendar for today. "
                "Use this when the user asks 'what's on my calendar today?', "
                "'מה יש לי היום ביומן?', or similar. No parameters required."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            now = datetime.now(timezone.utc)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            data = _api_get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                params={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 50,
                },
            )
            events = data.get("items", [])
            if not events:
                return ToolResult(
                    tool_name=self.spec.name, content="No events today.", success=True
                )
            text = "\n".join(_format_event(e) for e in events)
            return ToolResult(tool_name=self.spec.name, content=text, success=True)
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"calendar_today error: {exc}",
                success=False,
            )


@ToolRegistry.register("calendar_upcoming")
class CalendarUpcomingTool(BaseTool):
    """Return upcoming events for the next N days (default 7)."""

    tool_id = "calendar_upcoming"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_upcoming",
            description=(
                "Return upcoming Google Calendar events over the next N days. "
                "Defaults to the next 7 days."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days ahead to look (1-60).",
                        "default": 7,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum events to return.",
                        "default": 25,
                    },
                },
                "required": [],
            },
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        days = max(1, min(60, int(params.get("days", 7))))
        max_results = max(1, min(100, int(params.get("max_results", 25))))
        try:
            now = datetime.now(timezone.utc)
            end = now + timedelta(days=days)
            data = _api_get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                params={
                    "timeMin": now.isoformat(),
                    "timeMax": end.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": max_results,
                },
            )
            events = data.get("items", [])
            if not events:
                return ToolResult(
                    tool_name=self.spec.name,
                    content=f"No events in the next {days} days.",
                    success=True,
                )
            text = "\n".join(_format_event(e) for e in events)
            return ToolResult(tool_name=self.spec.name, content=text, success=True)
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"calendar_upcoming error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


def _decode_header(headers: List[Dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _format_message(msg: Dict[str, Any]) -> str:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    frm = _decode_header(headers, "From")
    subj = _decode_header(headers, "Subject") or "(no subject)"
    date = _decode_header(headers, "Date")
    snippet = msg.get("snippet", "").strip()
    return f"• From: {frm}\n  Subject: {subj}\n  Date: {date}\n  Snippet: {snippet}"


def _list_then_get_messages(
    query: str = "", label: str = "", max_results: int = 10
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"maxResults": max_results}
    if query:
        params["q"] = query
    if label:
        params["labelIds"] = label
    listing = _api_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params=params,
    )
    out: List[Dict[str, Any]] = []
    for m in listing.get("messages", []) or []:
        full = _api_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
            params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        )
        out.append(full)
    return out


@ToolRegistry.register("gmail_search")
class GmailSearchTool(BaseTool):
    """Search Gmail with the same query syntax as the Gmail search box."""

    tool_id = "gmail_search"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_search",
            description=(
                "Search the user's Gmail using Gmail query syntax "
                "(e.g. 'from:alice subject:report is:unread newer_than:7d'). "
                "Returns up to N messages with sender, subject, date, snippet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Gmail search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum messages to return (1-25).",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            category="communication",
        )

    def execute(self, **params: Any) -> ToolResult:
        query = (params.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_name=self.spec.name,
                content="gmail_search requires a non-empty 'query' parameter.",
                success=False,
            )
        max_results = max(1, min(25, int(params.get("max_results", 10))))
        try:
            msgs = _list_then_get_messages(query=query, max_results=max_results)
            if not msgs:
                return ToolResult(
                    tool_name=self.spec.name,
                    content=f"No messages match '{query}'.",
                    success=True,
                )
            text = "\n\n".join(_format_message(m) for m in msgs)
            return ToolResult(tool_name=self.spec.name, content=text, success=True)
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"gmail_search error: {exc}",
                success=False,
            )


@ToolRegistry.register("gmail_unread")
class GmailUnreadTool(BaseTool):
    """List recent unread Gmail messages in INBOX."""

    tool_id = "gmail_unread"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_unread",
            description=(
                "List the most recent unread emails from the user's Gmail INBOX. "
                "Use when asked 'do I have new emails?', 'מיילים שלא קראתי', etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum messages to return (1-25).",
                        "default": 10,
                    },
                },
                "required": [],
            },
            category="communication",
        )

    def execute(self, **params: Any) -> ToolResult:
        max_results = max(1, min(25, int(params.get("max_results", 10))))
        try:
            msgs = _list_then_get_messages(
                query="is:unread", label="INBOX", max_results=max_results
            )
            if not msgs:
                return ToolResult(
                    tool_name=self.spec.name,
                    content="No unread messages.",
                    success=True,
                )
            text = "\n\n".join(_format_message(m) for m in msgs)
            return ToolResult(tool_name=self.spec.name, content=text, success=True)
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"gmail_unread error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------


@ToolRegistry.register("drive_search")
class DriveSearchTool(BaseTool):
    """Search the user's Google Drive by file name or full-text content."""

    tool_id = "drive_search"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="drive_search",
            description=(
                "Search the user's Google Drive for files by name or content. "
                "Returns file titles, types, and shareable links."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum files to return (1-25).",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        query = (params.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_name=self.spec.name,
                content="drive_search requires 'query'.",
                success=False,
            )
        max_results = max(1, min(25, int(params.get("max_results", 10))))
        # Drive query syntax: name contains 'X' or fullText contains 'X'
        escaped = query.replace("'", "\\'")
        q = f"(name contains '{escaped}' or fullText contains '{escaped}') and trashed = false"
        try:
            data = _api_get(
                "https://www.googleapis.com/drive/v3/files",
                params={
                    "q": q,
                    "pageSize": max_results,
                    "fields": "files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName))",
                },
            )
            files = data.get("files", [])
            if not files:
                return ToolResult(
                    tool_name=self.spec.name,
                    content=f"No Drive files match '{query}'.",
                    success=True,
                )
            lines = []
            for f in files:
                name = f.get("name", "(untitled)")
                mime = f.get("mimeType", "")
                modified = f.get("modifiedTime", "")
                link = f.get("webViewLink", "")
                lines.append(f"• {name}  ({mime})\n  {modified}\n  {link}")
            return ToolResult(
                tool_name=self.spec.name, content="\n\n".join(lines), success=True
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"drive_search error: {exc}",
                success=False,
            )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@ToolRegistry.register("tasks_list")
class TasksListTool(BaseTool):
    """List the user's pending Google Tasks across all task lists."""

    tool_id = "tasks_list"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="tasks_list",
            description=(
                "Return the user's open Google Tasks (todos). Use when asked "
                "'what's on my todo list?', 'משימות פתוחות', etc."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="productivity",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            lists = _api_get("https://tasks.googleapis.com/tasks/v1/users/@me/lists")
            tasklists = lists.get("items", [])
            if not tasklists:
                return ToolResult(
                    tool_name=self.spec.name, content="No task lists.", success=True
                )
            output: List[str] = []
            for tl in tasklists:
                tl_id = tl["id"]
                tl_title = tl.get("title", "Tasks")
                items = _api_get(
                    f"https://tasks.googleapis.com/tasks/v1/lists/{tl_id}/tasks",
                    params={"showCompleted": "false", "maxResults": 50},
                )
                tasks = items.get("items", [])
                if not tasks:
                    continue
                output.append(f"## {tl_title}")
                for t in tasks:
                    title = t.get("title", "(no title)")
                    due = t.get("due", "")
                    suffix = f" — due {due}" if due else ""
                    output.append(f"• {title}{suffix}")
            if not output:
                return ToolResult(
                    tool_name=self.spec.name,
                    content="No open tasks.",
                    success=True,
                )
            return ToolResult(
                tool_name=self.spec.name, content="\n".join(output), success=True
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.spec.name,
                content=f"tasks_list error: {exc}",
                success=False,
            )


__all__ = [
    "CalendarTodayTool",
    "CalendarUpcomingTool",
    "GmailSearchTool",
    "GmailUnreadTool",
    "DriveSearchTool",
    "TasksListTool",
]

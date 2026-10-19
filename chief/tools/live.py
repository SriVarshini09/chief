"""Real integrations: Google Calendar and Gmail through Composio, web search through Parallel.

Return types match MockTools exactly, so skills do not know which one they are talking to.

VERIFIED (from SDK source: `composio` 0.28.0, `parallel-web` 1.3.5, and docs.composio.dev / docs.parallel.ai):
  - the Composio session calls (see composio_client.py) and the Parallel `client.search(...)` keyword names
    `objective`, `search_queries`, `mode`, `advanced_settings={"max_results", "source_policy": {"include_domains"}}`;
  - tool slugs and top-level argument names listed in docs/live-tools.md (copied from the toolkit pages).
ASSUMED (needs a run with real keys; no API key was available when this was written):
  - the inner shape of each tool's `data` (Google Calendar events look like the Google API: `items[]` with
    `id`, `summary`, `start.dateTime`, `attendees[].email`; Gmail messages carry `messageId`, `threadId`, `sender`,
    `subject`, `messageText`, `messageTimestamp`, `labelIds`). The parsers search a few nesting levels and accept
    several spellings, and a mismatch shows up as an empty list, so check `fetch_emails` / `list_events` first.
  - how GOOGLECALENDAR_CREATE_EVENT / PATCH_EVENT want their time strings (see `_when`).

TIME: the rest of Chief uses naive local datetimes. Anything coming back from Google is converted to local time
and the offset dropped; anything going out is treated as local. Local means the system time zone, or the IANA
zone named in CHIEF_TIMEZONE (e.g. America/Los_Angeles) when set.

SAFETY: this class does not decide when a write may run; ApprovalGate does (and ScopedTools hides writes from
skills). As a second line, write slugs live in a separate Composio session that is only created when a write
actually executes (see ComposioGateway).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from chief import health
from chief.config import Config
from chief.models import Email, Event, SleepDay
from chief.tools.base import Tools
from chief.tools.composio_client import ComposioGateway, LiveToolError, get

CAL = "googlecalendar"
MAIL = "gmail"

READ_SLUGS = {
    CAL: ["GOOGLECALENDAR_EVENTS_LIST"],
    MAIL: ["GMAIL_FETCH_EMAILS", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID", "GMAIL_CREATE_EMAIL_DRAFT"],
}
WRITE_SLUGS = {
    CAL: ["GOOGLECALENDAR_PATCH_EVENT", "GOOGLECALENDAR_CREATE_EVENT", "GOOGLECALENDAR_DELETE_EVENT"],
    MAIL: ["GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD"],
}

_PATCH_FIELDS = {"summary": "summary", "title": "summary", "description": "description", "location": "location"}
_MAX_PAGES = 4


def _find(data: Any, key: str, depth: int = 4) -> Any:
    """First value stored under `key`, looking through nested dicts (Composio wraps data inconsistently)."""
    if depth < 0 or not isinstance(data, dict):
        return None
    if key in data and data[key] is not None:
        return data[key]
    for value in data.values():
        found = _find(value, key, depth - 1)
        if found is not None:
            return found
    return None


def _find_list(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    for key in keys:
        found = _find(data, key)
        if isinstance(found, list):
            return found
    return []


def _first(d: dict, *keys: str) -> Any:
    for key in keys:
        if d.get(key) not in (None, ""):
            return d[key]
    return None


class LiveTools(Tools):
    def __init__(
        self,
        cfg: Config,
        gateway: ComposioGateway | None = None,
        search_client: Any = None,
        slack_client: Any = None,
        tz: tzinfo | None = None,
    ):
        self.cfg = cfg
        self.gateway = gateway or ComposioGateway(read_tools=READ_SLUGS, write_tools=WRITE_SLUGS)
        self._search_client = search_client
        self._slack_client = slack_client
        self.tz = tz or self._env_tz()
        self.calendar_id = os.getenv("CHIEF_CALENDAR_ID") or "primary"

    # ------------------------------------------------------------------ time
    @staticmethod
    def _env_tz() -> tzinfo | None:
        name = os.getenv("CHIEF_TIMEZONE")
        if not name:
            return None  # system local time
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)

    def _aware(self, dt: datetime) -> datetime:
        """Naive means local. Aware datetimes are returned unchanged."""
        if dt.tzinfo is not None:
            return dt
        return dt.replace(tzinfo=self.tz) if self.tz else dt.astimezone()

    def _naive_local(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt
        return (dt.astimezone(self.tz) if self.tz else dt.astimezone()).replace(tzinfo=None)

    def _parse_dt(self, value: Any) -> datetime | None:
        """ISO string, RFC 2822 string, or epoch seconds/milliseconds -> naive local datetime."""
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            n = float(value)
            return self._naive_local(datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc))
        try:
            return self._naive_local(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            try:
                return self._naive_local(parsedate_to_datetime(str(value)))
            except (TypeError, ValueError):
                return None

    def _when(self, value: datetime | str) -> dict:
        """Time arguments for create/patch. ASSUMED format: an ISO string with the offset included, plus
        `timezone` (IANA name) only when CHIEF_TIMEZONE is set. If Composio rejects the offset, send the
        naive string instead and rely on `timezone`."""
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        out = {"iso": self._aware(dt).isoformat(timespec="seconds")}
        key = getattr(self.tz, "key", None)
        if key:
            out["timezone"] = key
        return out

    # ------------------------------------------------------------------ calendar
    def _event(self, item: dict) -> Event | None:
        start = item.get("start") or {}
        end = item.get("end") or {}
        if item.get("status") == "cancelled" or "dateTime" not in start:
            return None  # cancelled, or an all-day entry (birthdays, holidays): not a meeting
        s, e = self._parse_dt(start["dateTime"]), self._parse_dt(end.get("dateTime"))
        if s is None or e is None:
            return None
        attendees = [a.get("email") or a.get("displayName") for a in item.get("attendees") or [] if isinstance(a, dict)]
        return Event(
            id=item["id"],
            title=item.get("summary") or "(no title)",
            start=s,
            end=e,
            description=item.get("description") or "",
            attendees=[a for a in attendees if a],
        )

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        """Timed events starting in [start, end), sorted. Same window rule as MockTools. All-day and cancelled
        events are left out. Recurring events come back as individual instances (singleEvents=True)."""
        events: dict[str, Event] = {}
        token = None
        for _ in range(_MAX_PAGES):
            data = self.gateway.execute(
                "read",
                "GOOGLECALENDAR_EVENTS_LIST",
                {
                    "calendarId": self.calendar_id,
                    "timeMin": self._aware(start).isoformat(),
                    "timeMax": self._aware(end).isoformat(),
                    "singleEvents": True,
                    "orderBy": "startTime",
                    "maxResults": 250,
                    "pageToken": token,
                },
            )
            for item in _find_list(data, "items", "events"):
                ev = self._event(item) if isinstance(item, dict) else None
                if ev and start <= ev.start < end:
                    events[ev.id] = ev
            token = _find(data, "nextPageToken")
            if not token:
                break
        return sorted(events.values(), key=lambda e: e.start)

    def find_free_slots(self, start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
        """Computed from list_events, not from GOOGLECALENDAR_FIND_FREE_SLOTS / FREE_BUSY_QUERY, on purpose:
        it keeps the MockTools rule that an "[Async]" event no longer blocks time, and avoids two response
        shapes that could not be checked without a key."""
        busy = [(e.start, e.end) for e in self.list_events(start, end) if "[Async]" not in e.title]
        slots, cursor = [], start
        for b0, b1 in busy:
            if (b0 - cursor).total_seconds() >= minutes * 60:
                slots.append((cursor, b0))
            cursor = max(cursor, b1)
        if (end - cursor).total_seconds() >= minutes * 60:
            slots.append((cursor, end))
        return slots

    # ------------------------------------------------------------------ email
    def _email(self, m: dict) -> Email:
        body = _first(m, "messageText", "snippet", "body") or (m.get("preview") or {}).get("body") or ""
        labels = m.get("labelIds")
        return Email(
            id=str(_first(m, "messageId", "id") or ""),
            thread_id=str(_first(m, "threadId", "thread_id") or ""),
            sender=str(_first(m, "sender", "from") or ""),
            subject=str(m.get("subject") or (m.get("preview") or {}).get("subject") or ""),
            snippet=re.sub(r"\s+", " ", str(body)).strip()[:500],
            received=self._parse_dt(_first(m, "messageTimestamp", "internalDate", "date")) or datetime(1970, 1, 1),
            unread=("UNREAD" in labels) if isinstance(labels, list) else True,
        )

    def fetch_emails(self, query: str = "", max_results: int = 10) -> list[Email]:
        """Newest first. `query` is Gmail search syntax (e.g. "is:unread newer_than:2d"), not a substring."""
        data = self.gateway.execute(
            "read",
            "GMAIL_FETCH_EMAILS",
            {"query": query or None, "max_results": max_results, "verbose": True, "user_id": "me"},
        )
        mails = [self._email(m) for m in _find_list(data, "messages") if isinstance(m, dict)]
        return sorted(mails, key=lambda m: m.received, reverse=True)[:max_results]

    def fetch_thread(self, thread_id: str) -> list[Email]:
        """Oldest first, so the last item is the message to reply to."""
        data = self.gateway.execute("read", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": thread_id, "user_id": "me"})
        mails = [self._email(m) for m in _find_list(data, "messages") if isinstance(m, dict)]
        for m in mails:
            m.thread_id = m.thread_id or thread_id
        return sorted(mails, key=lambda m: m.received)

    # ------------------------------------------------------------------ web
    @property
    def search_client(self) -> Any:
        if self._search_client is None:
            key = os.getenv("PARALLEL_API_KEY")
            if not key:
                raise LiveToolError("PARALLEL_API_KEY is not set (put it in .env); live web search needs it")
            from parallel import Parallel  # lazy: mock mode needs no extras

            self._search_client = Parallel(api_key=key)
        return self._search_client

    def search_web(
        self,
        objective: str,
        queries: list[str],
        max_results: int = 5,
        include_domains: list[str] | None = None,
    ) -> list[dict]:
        """Parallel Search in `fast` mode. `include_domains` (e.g. ["caltrain.com", "511.org"]) restricts results to
        those sites; if not passed, CHIEF_SEARCH_DOMAINS (comma separated) is used when set. Excerpts are web
        text, so treat them as untrusted data."""
        domains = include_domains or [d.strip() for d in os.getenv("CHIEF_SEARCH_DOMAINS", "").split(",") if d.strip()]
        advanced: dict[str, Any] = {"max_results": max_results}
        if domains:
            advanced["source_policy"] = {"include_domains": domains}
        try:
            res = self.search_client.search(
                objective=objective,
                search_queries=list(queries) or [objective],
                mode="fast",
                advanced_settings=advanced,
            )
        except LiveToolError:
            raise
        except Exception as exc:
            raise LiveToolError(f"Parallel search failed: {exc}") from exc
        return [
            {"title": get(r, "title") or get(r, "url"), "url": get(r, "url"), "excerpts": list(get(r, "excerpts") or [])}
            for r in (get(res, "results") or [])[:max_results]
        ]

    # ------------------------------------------------------------------ health and slack
    def read_health(self, days: int = 14) -> list[SleepDay]:
        """Reads the sleep CSV (date, sleep_hours, steps) from CHIEF_HEALTH_CSV or <data_dir>/sleep.csv.
        There is no health API in Composio, so this stays file based. A missing file is an error on purpose."""
        path = Path(os.getenv("CHIEF_HEALTH_CSV") or self.cfg.data_dir / "sleep.csv")
        if not path.exists():
            raise LiveToolError(
                f"no sleep CSV at {path}. Create the sample with: python -c \"from datetime import datetime; "
                f"from pathlib import Path; from chief import health; health.write_sample_csv(Path(r'{path}'), datetime.now())\""
            )
        rows = sorted(health.read_csv(path), key=lambda s: s.day)
        return rows[-(days + 1):]

    def post_slack(self, text: str) -> dict:
        if self._slack_client is None:
            token = os.getenv("SLACK_BOT_TOKEN")
            if not token:
                raise LiveToolError("SLACK_BOT_TOKEN is not set; cannot post to Slack")
            from slack_sdk import WebClient  # installed with slack-bolt

            self._slack_client = WebClient(token=token)
        channel = os.getenv("SLACK_CHANNEL")
        if not channel:
            raise LiveToolError("SLACK_CHANNEL is not set; cannot post to Slack")
        resp = self._slack_client.chat_postMessage(channel=channel, text=text)
        return {"ok": bool(get(resp, "ok", True))}

    # ------------------------------------------------------------------ drafts (safe write)
    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        data = self.gateway.execute(
            "read",  # the read session also holds the draft slug; it cannot send
            "GMAIL_CREATE_EMAIL_DRAFT",
            {"recipient_email": to, "subject": subject, "body": body, "thread_id": thread_id, "user_id": "me"},
        )
        return {"draft_id": _find(data, "draft_id") or _find(data, "id")}

    # ------------------------------------------------------------------ approval-only writes
    def patch_event(self, event_id: str, fields: dict) -> dict:
        """`fields` accepts summary (or title), description, location, start, end. Anything else is refused
        rather than silently dropped."""
        args: dict[str, Any] = {"event_id": event_id, "calendar_id": self.calendar_id}
        for name, value in fields.items():
            if name in _PATCH_FIELDS:
                args[_PATCH_FIELDS[name]] = value
            elif name in ("start", "end"):
                w = self._when(value)
                args["start_time" if name == "start" else "end_time"] = w["iso"]
                if "timezone" in w:
                    args["timezone"] = w["timezone"]
            else:
                raise ValueError(f"patch_event: unsupported field '{name}'")
        self.gateway.execute("write", "GOOGLECALENDAR_PATCH_EVENT", args)
        return {"id": event_id, "updated": True}

    def create_event(self, title: str, start: str, end: str, description: str = "") -> dict:
        s, e = self._when(start), self._when(end)
        data = self.gateway.execute(
            "write",
            "GOOGLECALENDAR_CREATE_EVENT",
            {
                "calendar_id": self.calendar_id,
                "summary": title,
                "start_datetime": s["iso"],
                "end_datetime": e["iso"],
                "timezone": s.get("timezone"),
                "description": description or None,
                "send_updates": "none",
            },
        )
        return {"id": _find(data, "id") or _find(data, "event_id")}

    def delete_event(self, event_id: str) -> dict:
        self.gateway.execute(
            "write", "GOOGLECALENDAR_DELETE_EVENT", {"event_id": event_id, "calendar_id": self.calendar_id}
        )
        return {"deleted": True}

    def send_email(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        """With a thread_id this replies in that thread (GMAIL_REPLY_TO_THREAD, which is not one of the five slugs
        in the brief but is the only documented way to keep the thread); otherwise GMAIL_SEND_EMAIL."""
        if thread_id:
            self.gateway.execute(
                "write",
                "GMAIL_REPLY_TO_THREAD",
                {"thread_id": thread_id, "recipient_email": to, "message_body": body, "user_id": "me"},
            )
        else:
            self.gateway.execute(
                "write", "GMAIL_SEND_EMAIL", {"recipient_email": to, "subject": subject, "body": body, "user_id": "me"}
            )
        return {"sent": True}


# --- Safe local actions (chief/tools/local.py): the real opener (os.startfile on Windows), allow-listed paths only.
from chief.tools.local import LocalActions  # noqa: E402


def _live_local(self: LiveTools) -> LocalActions:
    if "_local" not in self.__dict__:
        self._local = LocalActions(self.cfg.local_roots or None, state_dir=self.cfg.state_dir)
    return self._local


LiveTools.local = property(_live_local)
LiveTools.list_folder = lambda self, path: self.local.list_folder(path)
LiveTools.open_folder = lambda self, path: self.local.open_folder(path)
LiveTools.open_file = lambda self, path: self.local.open_file(path)

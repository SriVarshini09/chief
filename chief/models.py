"""Shared data types. Everything that crosses a module boundary is defined here."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime


def clock(d: datetime) -> str:
    """9:30 AM. Built by hand because strftime's no-padding flag differs between Windows and Unix."""
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def when(value: object) -> str | None:
    """'Thu Oct 22, 2:00 PM' for an ISO string or datetime, None if it cannot be read."""
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return f"{d:%a %b} {d.day}, {clock(d)}"


def _plain_event_change(args: dict) -> str | None:
    fields = args.get("fields")
    if set(args) != {"event_id", "fields"} or not isinstance(fields, dict) or not fields or set(fields) - {"summary", "description", "start", "end"}:
        return None
    parts = []
    title = fields.get("summary")
    if title:
        title = str(title)
        if title.startswith("[Async] "):
            parts.append(f'Mark "{title[8:]}" as async, so it becomes a written update')
        elif title.startswith("Walk: "):
            parts.append(f'Make "{title[6:]}" a walking call')
        else:
            parts.append(f'Rename a calendar event to "{title}"')
    for key, verb in (("start", "start at"), ("end", "end at")):
        if key in fields:
            moved = when(fields[key])
            if moved is None:
                return None
            parts.append(f"{verb} {moved}")
    if not parts:
        return "Update the notes on a calendar event"
    return ", ".join(parts)


def _plain_mail(verb: str, args: dict) -> str | None:
    if set(args) - {"to", "subject", "body", "thread_id"} or not args.get("to"):
        return None
    return f"{verb} to {args['to']}\nSubject: {args.get('subject', '')}\n\n{args.get('body', '')}"


@dataclass
class ToolCall:
    tool: str
    args: dict

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"{self.tool}({shown})"

    def plain(self) -> str:
        """What this does, for the person approving it. The approval depends on this being true, so an
        email shows its whole text, and anything not fully understood falls back to `describe()`."""
        return self._plain_or_none() or self.describe()

    def _plain_or_none(self) -> str | None:
        args = self.args
        if self.tool == "create_event" and not set(args) - {"title", "start", "end", "description"}:
            start, end = when(args.get("start")), when(args.get("end"))
            if args.get("title") and start and end:
                if start.split(", ")[0] == end.split(", ")[0]:  # same day: say the date once
                    end = end.split(", ")[1]
                return f'Add "{args["title"]}" to your calendar: {start} to {end}'
        if self.tool == "patch_event":
            return _plain_event_change(args)
        if self.tool == "delete_event" and set(args) == {"event_id"}:
            return f"Delete calendar event {args['event_id']}"
        if self.tool == "send_email":
            return _plain_mail("Send this email", args)
        if self.tool == "create_draft":
            return _plain_mail("Save this draft", args)
        return None


@dataclass
class Proposal:
    """Something Chief wants to do. Nothing in `actions` runs until it is approved."""

    skill: str
    summary: str
    actions: list[ToolCall]
    key: str  # dedupe key: the same situation never produces a second card
    sources: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    attendees: list[str] = field(default_factory=list)

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class SleepDay:
    day: date  # the morning the sleep ended
    hours: float
    steps: int | None = None


@dataclass
class Email:
    id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received: datetime
    unread: bool = True
    bulk: bool = False  # sent to a list: newsletters, promotions, notifications

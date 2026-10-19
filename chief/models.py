"""Shared data types. Everything that crosses a module boundary is defined here."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class ToolCall:
    tool: str
    args: dict

    def describe(self) -> str:
        shown = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"{self.tool}({shown})"


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

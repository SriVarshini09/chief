"""Everything Chief does, as a stream anyone can follow.

The Stage screen polls it (`since(seq)`); the real-window layer subscribes to it. Both see the same
events, so what is on screen is exactly what Chief is doing.

kind values:
  heard      what you said
  step       Chief starts or finishes a piece of work ("Running audit")
  working    a tool call has started (the result event with the same detail["ref"] follows)
  read       a read tool finished: detail["result"] holds a compact, JSON-safe summary
  write      a write tool finished (only ever after approval, or a safe write such as a draft)
  proposal   a proposal was created
  approved   you approved it
  skipped    you skipped it
  say        what Chief answers
  error      something failed
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator

# The task the current thread is working on. Set with task_scope(); emit() stamps it on every event as
# detail["task"], so the Stage can draw one lane per task. See docs/events.md.
current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar("chief_task", default=None)


@contextmanager
def task_scope(task_id: str | None) -> Iterator[None]:
    token = current_task.set(task_id)
    try:
        yield
    finally:
        current_task.reset(token)


@dataclass
class Activity:
    seq: int
    at: str
    kind: str
    title: str
    tool: str | None = None
    detail: dict = field(default_factory=dict)
    ok: bool = True

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "title": self.title,
            "tool": self.tool,
            "detail": self.detail,
            "ok": self.ok,
        }


class ActivityBus:
    def __init__(self, limit: int = 500):
        self._events: list[Activity] = []
        self._subscribers: list[Callable[[Activity], None]] = []
        self._limit = limit
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, kind: str, title: str, *, tool: str | None = None, detail: dict | None = None, ok: bool = True) -> Activity:
        detail = dict(detail or {})
        task = current_task.get()
        if task and "task" not in detail:
            detail["task"] = task
        with self._lock:
            self._seq += 1
            event = Activity(self._seq, datetime.now().isoformat(timespec="milliseconds"), kind, title, tool, detail, ok)
            self._events.append(event)
            if len(self._events) > self._limit:
                del self._events[: len(self._events) - self._limit]
            subscribers = list(self._subscribers)
        for fn in subscribers:  # outside the lock; a broken listener must never break Chief
            try:
                fn(event)
            except Exception:
                pass
        return event

    def since(self, seq: int = 0) -> list[Activity]:
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    def subscribe(self, fn: Callable[[Activity], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

"""RecordingTools wraps any Tools and reports every call to the ActivityBus.

Sits between the runtime and the real tools, so skills need no changes: each call becomes a
"working" event, then a result event with a compact summary (never full email bodies).
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
from datetime import date, datetime
from typing import Any

from chief.activity import ActivityBus
from chief.tools.base import ALL_TOOLS, READ_TOOLS, Tools

_MAX_ITEMS = 20
_MAX_TEXT = 400
_refs = itertools.count(1)

_WORKING = {
    "list_events": "Reading your calendar",
    "find_free_slots": "Checking when you are free",
    "fetch_emails": "Reading your email",
    "fetch_thread": "Reading an email thread",
    "search_web": "Searching the web",
    "read_health": "Reading your sleep data",
    "create_draft": "Saving a draft",
    "post_slack": "Posting to Slack",
    "patch_event": "Updating a calendar event",
    "create_event": "Creating a calendar event",
    "delete_event": "Deleting a calendar event",
    "send_email": "Sending an email",
    "list_folder": "Looking inside a folder",
    "open_folder": "Opening a folder on screen",
    "open_file": "Opening a document on screen",
}


def _jsonable(obj: Any, depth: int = 0) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, str):
        return obj if len(obj) <= _MAX_TEXT else obj[:_MAX_TEXT] + "..."
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    if depth > 4:
        return str(obj)[:_MAX_TEXT]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v, depth + 1) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(obj.items())[:_MAX_ITEMS]}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(obj)[:_MAX_ITEMS]]
    return str(obj)[:_MAX_TEXT]


def _leaf(path: Any) -> str:
    text = str(path or "").replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1][:80] or "a folder"


def _title(name: str, args: dict, result: Any) -> str:
    n = len(result) if isinstance(result, (list, tuple)) else None
    if name == "list_events":
        return f"Read your calendar: {n} event(s)"
    if name == "find_free_slots":
        return f"Checked free time: {n} slot(s)"
    if name == "fetch_emails":
        return f"Read your email: {n} message(s)"
    if name == "fetch_thread":
        return f"Read an email thread: {n} message(s)"
    if name == "search_web":
        return f"Searched the web: {str(args.get('objective', ''))[:80]}"
    if name == "read_health":
        return f"Read your sleep data: {n} day(s)"
    if name == "create_draft":
        return f"Saved a draft to {args.get('to', '')}"
    if name == "post_slack":
        return "Posted to Slack"
    if name == "patch_event":
        return f"Updated calendar event: {_jsonable(args.get('fields', {})).get('summary', args.get('event_id'))}"
    if name == "create_event":
        return f"Created calendar event: {args.get('title', '')}"
    if name == "delete_event":
        return "Deleted a calendar event"
    if name == "list_folder":
        return f"Looked inside {_leaf(args.get('path'))}: {result.get('count', 0) if isinstance(result, dict) else 0} item(s)"
    if name == "open_folder":
        return f"Opened the folder {_leaf(args.get('path'))}"
    if name == "open_file":
        return f"Opened the document {_leaf(args.get('path'))}"
    return f"Sent an email to {args.get('to', '')}"


def _make(name: str):
    kind = "read" if name in READ_TOOLS else "write"
    signature = inspect.signature(getattr(Tools, name))

    def method(self, *args, **kwargs):
        try:
            bound = signature.bind(None, *args, **kwargs)
            call_args = {k: v for k, v in bound.arguments.items() if k != "self"}
        except TypeError:
            call_args = {"args": list(args), **kwargs}
        ref = next(_refs)
        safe_args = _jsonable(call_args)
        self.bus.emit("working", _WORKING[name] + "...", tool=name, detail={"ref": ref, "args": safe_args})
        try:
            result = getattr(self.inner, name)(*args, **kwargs)
        except Exception as exc:
            self.bus.emit("error", f"{_WORKING[name]} failed: {exc}", tool=name, ok=False, detail={"ref": ref, "args": safe_args})
            raise
        title, detail = _title(name, call_args, result), {"ref": ref, "args": safe_args, "result": _jsonable(result)}
        # failover/replay tools (chief/tools/replay.py) flag results that did not come live: never hide that
        meta = getattr(self.inner, "pop_meta", lambda: {})()
        if meta.get("cached"):
            title += " (cached)"
        elif meta.get("simulated"):
            title += " (simulated)"
        detail.update(meta)
        self.bus.emit(kind, title, tool=name, detail=detail)
        return result

    method.__name__ = name
    return method


class RecordingTools(Tools):
    def __init__(self, inner: Tools, bus: ActivityBus):
        self.inner, self.bus = inner, bus


for _name in ALL_TOOLS:
    setattr(RecordingTools, _name, _make(_name))

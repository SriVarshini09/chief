"""Record, replay and fail over: the live demo must survive a dead Wi-Fi or a slow API.

Three pieces, all honest about what they are doing:

  RecorderTools   wraps the real tools and saves each READ result into the demo cache
                  (data/demo_cache.json), keyed by (tool, normalized args). Only the fields the skills use
                  are kept, email text only as the snippets they already see, and anything that looks like
                  a secret is redacted before it touches disk. Writes are passed through untouched and are
                  never cached.
  ReplayTools     serves cached reads. Every answer is flagged cached. Writes are NEVER replayed: they raise,
                  unless a `writes` backend (MockTools) was given explicitly (mode `demo-offline`), in which
                  case the result is flagged simulated.
  FailoverTools   tries the live tools with a short timeout. On any failure or timeout a read is served from
                  the cache and flagged cached. A write is never faked: if live fails, it raises a clear error.

"Flagged" means: after each call, `pop_meta()` returns {"cached": True, ...} or {"simulated": True}, and
RecordingTools copies that into the activity event's detail (and adds " (cached)" to the title), so the
Stage can show a small tag and nothing cached is ever presented as live.
"""

from __future__ import annotations

import difflib
import inspect
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from chief.models import Email, Event, SleepDay
from chief.tools.base import ALL_TOOLS, READ_TOOLS, Tools

CACHE_VERSION = 1
DEFAULT_TIMEOUT = 6.0
WRITE_TIMEOUT = 30.0
BREAKER_SECONDS = 30.0  # after a live failure, go straight to the cache for this long, then probe again
SIMILARITY = 0.6  # how alike two web-search requests must be to share a cached answer

_MAX_SNIPPET = 600
_MAX_EXCERPT = 1000
_MAX_EXCERPTS = 8
_MAX_TITLE = 300
_MAX_RESULTS = 50


# Local-machine tools (chief/tools/local.py): they never touch the network, so there is nothing to cache, time out
# or fail over. They always go straight to the underlying tools, unflagged, and never feed the breaker.
LOCAL_TOOLS = {"list_folder", "open_folder", "open_file"}


class CacheMiss(LookupError):
    """Nothing usable in the demo cache for this call."""


class WriteNotAllowed(RuntimeError):
    """A write was refused because there is no live service to run it and writes are never faked."""


# --------------------------------------------------------------------------- secrets

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(pwd|passcode|password|token|api[_-]?key|apikey|secret|sig|signature|auth|access_token)=[^&\s\"'<>]+"), r"\1=REDACTED"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer REDACTED"),
    (re.compile(r"\b(?:sk|gsk|csk|pk|rk|xox[abpr]|xapp|ghp|ak)[-_][A-Za-z0-9_-]{16,}"), "REDACTED"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "REDACTED"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "REDACTED"),  # JWTs
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _clip(text: Any, limit: int) -> str:
    text = redact(str(text or ""))
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------- keys

_CLOCK = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?", re.I)
_MONTH_DAY = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", re.I)
_WEEKDAY = re.compile(r"\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b", re.I)


def _squash(text: str) -> str:
    """Lower-case a search request and blank out the clock times and dates in it, so the same question asked
    at 5:41 PM and at 5:43 PM (or on a different day) lands on the same cache entry."""
    text = _CLOCK.sub("<time>", str(text))
    text = _MONTH_DAY.sub("<date>", text)
    text = _WEEKDAY.sub("<day>", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0, tzinfo=None).isoformat(timespec="minutes")
    if isinstance(value, str):
        try:
            return _when(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return value
    return str(value)


def _bound(tool: str, args: tuple, kwargs: dict) -> dict:
    signature = inspect.signature(getattr(Tools, tool))
    bound = signature.bind(None, *args, **kwargs)
    bound.apply_defaults()
    return {k: v for k, v in bound.arguments.items() if k != "self"}


def normalize_args(tool: str, args: dict) -> dict:
    """The part of a call that decides which cached answer it gets. JSON-safe and stable."""
    if tool in ("list_events", "find_free_slots"):
        out = {"start": _when(args["start"]), "end": _when(args["end"])}
        if tool == "find_free_slots":
            out["minutes"] = int(args["minutes"])
        return out
    if tool == "fetch_emails":
        return {"query": str(args.get("query") or "").strip().lower(), "max_results": int(args.get("max_results") or 10)}
    if tool == "fetch_thread":
        return {"thread_id": str(args["thread_id"]).strip()}
    if tool == "search_web":
        return {
            "objective": _squash(args["objective"]),
            "queries": [_squash(q) for q in (args.get("queries") or [])],
            "max_results": int(args.get("max_results") or 5),
        }
    if tool == "read_health":
        return {"days": int(args.get("days") or 14)}
    raise ValueError(f"{tool} is not a read tool and is never cached")


def cache_key(tool: str, norm: dict) -> str:
    return f"{tool}:{json.dumps(norm, sort_keys=True, separators=(',', ':'), ensure_ascii=False)}"


# --------------------------------------------------------------------------- codec (only what skills use)


def encode(tool: str, result: Any) -> Any:
    """Result to JSON. Keeps only the fields the skills read; email text only as the snippet they already use."""
    if tool == "list_events":
        return [
            {"id": e.id, "title": _clip(e.title, _MAX_TITLE), "start": e.start.isoformat(), "end": e.end.isoformat(),
             "attendees": [_clip(a, 200) for a in e.attendees]}
            for e in list(result)[:_MAX_RESULTS * 10]
        ]
    if tool == "find_free_slots":
        return [[a.isoformat(), b.isoformat()] for a, b in list(result)[:_MAX_RESULTS * 10]]
    if tool in ("fetch_emails", "fetch_thread"):
        return [
            {"id": m.id, "thread_id": m.thread_id, "sender": _clip(m.sender, 200), "subject": _clip(m.subject, _MAX_TITLE),
             "snippet": _clip(m.snippet, _MAX_SNIPPET), "received": m.received.isoformat(), "unread": bool(m.unread)}
            for m in list(result)[:_MAX_RESULTS]
        ]
    if tool == "search_web":
        return [
            {"title": _clip(r.get("title", ""), _MAX_TITLE), "url": redact(str(r.get("url") or "")),
             "excerpts": [_clip(x, _MAX_EXCERPT) for x in list(r.get("excerpts") or [])[:_MAX_EXCERPTS]]}
            for r in list(result)[:_MAX_RESULTS]
        ]
    if tool == "read_health":
        return [{"day": s.day.isoformat(), "hours": s.hours, "steps": s.steps} for s in result]
    raise ValueError(f"{tool} is not a read tool and is never cached")


def decode(tool: str, data: Any) -> Any:
    if tool == "list_events":
        return [Event(d["id"], d["title"], datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]),
                      "", list(d.get("attendees") or [])) for d in data]
    if tool == "find_free_slots":
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in data]
    if tool in ("fetch_emails", "fetch_thread"):
        return [Email(d["id"], d["thread_id"], d["sender"], d["subject"], d["snippet"],
                      datetime.fromisoformat(d["received"]), bool(d.get("unread", True))) for d in data]
    if tool == "search_web":
        return [{"title": d["title"], "url": d["url"], "excerpts": list(d["excerpts"])} for d in data]
    if tool == "read_health":
        return [SleepDay(date.fromisoformat(d["day"]), float(d["hours"]), d.get("steps")) for d in data]
    raise ValueError(f"{tool} is not a read tool and is never cached")


# --------------------------------------------------------------------------- the cache file


def _free_slots(events: list[Event], start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
    """Same rule as MockTools.find_free_slots and LiveTools: [Async] events do not block."""
    busy = sorted((e.start, e.end) for e in events if "[Async]" not in e.title and e.start >= start and e.start < end)
    slots, cursor = [], start
    for b0, b1 in busy:
        if (b0 - cursor).total_seconds() >= minutes * 60:
            slots.append((cursor, b0))
        cursor = max(cursor, b1)
    if (end - cursor).total_seconds() >= minutes * 60:
        slots.append((cursor, end))
    return slots


class DemoCache:
    """data/demo_cache.json: {"version", "recorded_at", "entries": {key: {tool, args, recorded_at, result}}}."""

    def __init__(self, path: Path | str, entries: dict | None = None, recorded_at: str | None = None):
        self.path = Path(path)
        self.entries: dict[str, dict] = entries or {}
        self.recorded_at = recorded_at
        self._lock = threading.Lock()

    # -- file
    @classmethod
    def load(cls, path: Path | str) -> "DemoCache":
        path = Path(path)
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path)  # a corrupt cache is treated as empty (doctor reports it)
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return cls(path)
        return cls(path, dict(raw.get("entries") or {}), raw.get("recorded_at"))

    def save(self) -> None:
        with self._lock:
            self.recorded_at = datetime.now().isoformat(timespec="seconds")
            payload = {"version": CACHE_VERSION, "recorded_at": self.recorded_at, "entries": self.entries}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)  # never leave a half-written cache for the stage

    # -- info
    def __len__(self) -> int:
        return len(self.entries)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries.values():
            out[e["tool"]] = out.get(e["tool"], 0) + 1
        return out

    def age(self, now: datetime | None = None) -> timedelta | None:
        if not self.recorded_at:
            return None
        try:
            return (now or datetime.now()) - datetime.fromisoformat(self.recorded_at)
        except ValueError:
            return None

    # -- write
    def put(self, tool: str, args: dict, result: Any) -> str:
        norm = normalize_args(tool, args)
        key = cache_key(tool, norm)
        with self._lock:
            self.entries[key] = {
                "tool": tool, "args": norm, "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "result": encode(tool, result),
            }
        return key

    # -- read
    def _of(self, tool: str) -> list[dict]:
        with self._lock:
            return [e for e in self.entries.values() if e["tool"] == tool]

    def _events_between(self, start: datetime, end: datetime) -> list[Event] | None:
        """All cached events starting in [start, end), if any cached window overlaps it; else None."""
        s, e = start.replace(tzinfo=None), end.replace(tzinfo=None)
        overlapping = [x for x in self._of("list_events")
                       if datetime.fromisoformat(x["args"]["start"]) < e and datetime.fromisoformat(x["args"]["end"]) > s]
        if not overlapping:
            return None
        seen: dict[str, Event] = {}
        for entry in overlapping:
            for ev in decode("list_events", entry["result"]):
                seen[ev.id] = ev
        return sorted((v for v in seen.values() if s <= v.start < e), key=lambda v: v.start)

    def lookup(self, tool: str, args: dict) -> tuple[Any, str, str]:
        """(result, how, recorded_at). `how` is exact | derived | similar. Raises CacheMiss.

        The demo runs at a different time than the recording, so time-based and wording-based calls fall back
        to the closest honest answer instead of failing: calendar windows are rebuilt from every cached event
        that falls inside them, free slots are computed from those, emails are filtered by the query, and web
        searches match on similar wording. All of it is still flagged cached."""
        norm = normalize_args(tool, args)
        with self._lock:
            hit = self.entries.get(cache_key(tool, norm))
        if hit:
            return decode(tool, hit["result"]), "exact", hit["recorded_at"]
        stamp = self.recorded_at or ""

        if tool == "list_events":
            evs = self._events_between(args["start"], args["end"])
            if evs is not None:
                return evs, "derived", stamp
        elif tool == "find_free_slots":
            evs = self._events_between(args["start"], args["end"])
            if evs is not None:
                return _free_slots(evs, args["start"], args["end"], int(args["minutes"])), "derived", stamp
        elif tool == "fetch_emails":
            q, n = norm["query"], norm["max_results"]
            for entry in self._of("fetch_emails"):
                if entry["args"]["query"] == "" and q:  # filter the unfiltered fetch, like the mock does
                    mails = [m for m in decode(tool, entry["result"]) if q in f"{m.sender} {m.subject} {m.snippet}".lower()]
                    return mails[:n], "derived", entry["recorded_at"]
            for entry in self._of("fetch_emails"):
                if entry["args"]["query"] == q:
                    return decode(tool, entry["result"])[:n], "derived", entry["recorded_at"]
        elif tool == "fetch_thread":
            tid = norm["thread_id"]
            found: dict[str, Email] = {}
            for entry in self._of("fetch_emails") + self._of("fetch_thread"):
                for m in decode("fetch_emails", entry["result"]):
                    if m.thread_id == tid:
                        found[m.id] = m
            if found:
                return sorted(found.values(), key=lambda m: m.received), "derived", stamp
        elif tool == "search_web":
            want = norm["objective"] + " " + " ".join(norm["queries"])
            best, score = None, 0.0
            for entry in self._of("search_web"):
                have = entry["args"]["objective"] + " " + " ".join(entry["args"]["queries"])
                ratio = difflib.SequenceMatcher(None, want, have).ratio()
                if ratio > score:
                    best, score = entry, ratio
            if best and score >= SIMILARITY:
                return decode(tool, best["result"])[: norm["max_results"]], "similar", best["recorded_at"]
        elif tool == "read_health":
            for entry in sorted(self._of("read_health"), key=lambda x: -x["args"]["days"]):
                if entry["args"]["days"] >= norm["days"]:
                    days = decode(tool, entry["result"])
                    if days:
                        cutoff = max(d.day for d in days) - timedelta(days=norm["days"])
                        return [d for d in days if d.day >= cutoff], "derived", entry["recorded_at"]
        raise CacheMiss(f"nothing cached for {tool}({json.dumps(norm, ensure_ascii=False)[:160]})")


# --------------------------------------------------------------------------- tool wrappers


class _MetaMixin:
    """Per-thread notes about the last call (cached / simulated), read once by RecordingTools."""

    def _init_meta(self) -> None:
        self._meta = threading.local()

    def _set_meta(self, meta: dict) -> None:
        self._meta.value = meta

    def pop_meta(self) -> dict:
        meta = getattr(self._meta, "value", None) or {}
        self._meta.value = {}
        return meta


def _cached_meta(how: str, recorded_at: str, reason: str = "") -> dict:
    meta = {"cached": True, "cache_match": how, "cached_at": recorded_at}
    if reason:
        meta["cache_reason"] = reason
    return meta


class RecorderTools(Tools, _MetaMixin):
    """Wraps real tools. Reads run and are saved; writes are passed through (or refused with `allow_writes=False`,
    the setting `record-demo` uses so recording can never change an account)."""

    def __init__(self, inner: Tools, cache: DemoCache, allow_writes: bool = True):
        self.inner, self.cache, self.allow_writes = inner, cache, allow_writes
        self.recorded: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self._init_meta()

    def call_read(self, name: str, *args, **kwargs):
        call_args = _bound(name, args, kwargs)
        try:
            result = getattr(self.inner, name)(*args, **kwargs)
        except Exception as exc:
            self.failed.append((name, f"{type(exc).__name__}: {exc}"))
            raise
        self.recorded.append(self.cache.put(name, call_args, result))
        return result


def _recorder_read(name: str):
    def method(self, *args, **kwargs):
        return self.call_read(name, *args, **kwargs)

    method.__name__ = name
    return method


def _recorder_write(name: str, always: bool = False):
    def method(self, *args, **kwargs):
        if not self.allow_writes and not always:
            raise WriteNotAllowed(f"{name} is not run while recording the demo cache; nothing was changed")
        return getattr(self.inner, name)(*args, **kwargs)

    method.__name__ = name
    return method


for _n in ALL_TOOLS:
    if _n == "list_folder":
        setattr(RecorderTools, _n, _recorder_write(_n, always=True))  # straight through, never cached
    else:
        setattr(RecorderTools, _n, _recorder_read(_n) if _n in READ_TOOLS else _recorder_write(_n))


class ReplayTools(Tools, _MetaMixin):
    """Cached reads only. Writes raise, unless `writes` (a MockTools) is given: then they run there and are
    flagged simulated, because nothing real happened."""

    def __init__(self, cache: DemoCache, writes: Tools | None = None, reason: str = ""):
        self.cache, self.writes, self.reason = cache, writes, reason
        self.simulated: list[tuple[str, dict]] = []
        self._overlay: list[tuple[str, dict, Any]] = []  # simulated calendar writes, so later reads show them
        self._init_meta()

    def _with_overlay(self, events: list[Event], start: datetime, end: datetime) -> list[Event]:
        """Show this run's simulated calendar changes on top of the cached calendar (offline rehearsal only)."""
        for name, a, res in self._overlay:
            if name == "patch_event":
                for e in events:
                    if e.id == a["event_id"]:
                        e.title = a["fields"].get("summary") or a["fields"].get("title") or e.title
            elif name == "delete_event":
                events = [e for e in events if e.id != a["event_id"]]
            elif name == "create_event":
                s, e_ = datetime.fromisoformat(a["start"]), datetime.fromisoformat(a["end"])
                if start.replace(tzinfo=None) <= s < end.replace(tzinfo=None):
                    events.append(Event(str((res or {}).get("id") or f"sim-{len(events)}"), a["title"], s, e_, a.get("description", "")))
        return sorted(events, key=lambda e: e.start)

    def serve(self, name: str, *args, **kwargs):
        call_args = _bound(name, args, kwargs)
        try:
            result, how, stamp = self.cache.lookup(name, call_args)
        except CacheMiss as exc:
            raise CacheMiss(f"{exc}. Run `python -m chief record-demo` while online to fill the cache.") from None
        if name == "list_events" and self._overlay:
            result = self._with_overlay(result, call_args["start"], call_args["end"])
        self._set_meta(_cached_meta(how, stamp, self.reason))
        return result

    def simulate(self, name: str, *args, **kwargs):
        call_args = _bound(name, args, kwargs)
        if self.writes is None:
            raise WriteNotAllowed(
                f"{name}: no live service is available, so this was NOT done (Chief never replays or fakes a write). "
                "Use CHIEF_MODE=demo-offline to rehearse writes as simulated."
            )
        try:
            result = getattr(self.writes, name)(*args, **kwargs)
        except KeyError:  # the mock has never heard of this (real) event id: acknowledge, change nothing
            result = {"id": call_args.get("event_id"), "updated": True}
        if isinstance(result, dict):
            result = {**result, "simulated": True}
        self.simulated.append((name, call_args))
        self._overlay.append((name, call_args, result))
        self._set_meta({"simulated": True})
        return result


def _replay_read(name: str):
    def method(self, *args, **kwargs):
        return self.serve(name, *args, **kwargs)

    method.__name__ = name
    return method


def _replay_local(name: str):
    def method(self, *args, **kwargs):
        if self.writes is None:
            raise RuntimeError(f"{name} needs local tools, and this replay has none")
        self._set_meta({})
        return getattr(self.writes, name)(*args, **kwargs)  # local and read-only: not cached, not flagged

    method.__name__ = name
    return method


def _replay_write(name: str):
    def method(self, *args, **kwargs):
        return self.simulate(name, *args, **kwargs)

    method.__name__ = name
    return method


for _n in ALL_TOOLS:
    if _n == "list_folder":
        setattr(ReplayTools, _n, _replay_local(_n))
    else:
        setattr(ReplayTools, _n, _replay_read(_n) if _n in READ_TOOLS else _replay_write(_n))


class FailoverTools(Tools, _MetaMixin):
    """Live first, cache second, never a fake write.

    Reads: call `live` on a worker thread with `timeout` seconds. Any exception or timeout serves the cached
    result (flagged cached) and opens a short breaker so the next reads do not each wait out the timeout.
    Writes: always go to `live`. If live fails the error is raised as is, with a note; nothing is replayed,
    faked or retried. A write that times out may still complete later, and the error says so.
    """

    def __init__(
        self,
        live: Tools,
        replay: ReplayTools,
        timeout: float = DEFAULT_TIMEOUT,
        write_timeout: float = WRITE_TIMEOUT,
        breaker_seconds: float = BREAKER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.live, self.replay = live, replay
        self.timeout, self.write_timeout, self.breaker_seconds, self.clock = timeout, write_timeout, breaker_seconds, clock
        self._down_until = 0.0
        self.last_error: str | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chief-live")
        self._init_meta()

    @property
    def live_ok(self) -> bool:
        return self.clock() >= self._down_until

    def _run(self, fn: Callable, timeout: float):
        future = self._pool.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError(f"no answer from the live service within {timeout:g}s") from None

    def read(self, name: str, *args, **kwargs):
        self._set_meta({})
        reason = ""
        if self.live_ok:
            try:
                result = self._run(lambda: getattr(self.live, name)(*args, **kwargs), self.timeout)
                self.last_error = None
                return result
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"[:200]
                self.last_error = reason
                self._down_until = self.clock() + self.breaker_seconds
        else:
            reason = self.last_error or "live service marked down"
        self.replay.reason = reason
        try:
            result = self.replay.serve(name, *args, **kwargs)
            self._set_meta(self.replay.pop_meta())
            return result
        except CacheMiss as miss:
            raise RuntimeError(f"{name} failed live ({reason}) and the demo cache has no answer: {miss}") from None

    def write(self, name: str, *args, **kwargs):
        self._set_meta({})
        try:
            return self._run(lambda: getattr(self.live, name)(*args, **kwargs), self.write_timeout)
        except TimeoutError as exc:
            self._down_until = self.clock() + self.breaker_seconds
            raise RuntimeError(
                f"{name}: {exc}. It may still have gone through; check before trying again. Nothing was faked."
            ) from None
        except Exception as exc:
            raise RuntimeError(f"{name} failed live ({type(exc).__name__}: {exc}). It was NOT done and was not faked.") from exc

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _failover_read(name: str):
    def method(self, *args, **kwargs):
        return self.read(name, *args, **kwargs)

    method.__name__ = name
    return method


def _failover_local(name: str):
    def method(self, *args, **kwargs):
        self._set_meta({})
        return getattr(self.live, name)(*args, **kwargs)  # no timeout, no breaker, no cache, errors as they are

    method.__name__ = name
    return method


def _failover_write(name: str):
    def method(self, *args, **kwargs):
        return self.write(name, *args, **kwargs)

    method.__name__ = name
    return method


for _n in ALL_TOOLS:
    if _n in LOCAL_TOOLS:
        setattr(FailoverTools, _n, _failover_local(_n))
    else:
        setattr(FailoverTools, _n, _failover_read(_n) if _n in READ_TOOLS else _failover_write(_n))


# --------------------------------------------------------------------------- what to record

# The demo story, in order. `record` False = an approval or a write: said on stage, never run while recording.
STORY: list[tuple[str, bool]] = [
    ("What's in my inbox?", True),
    ("Reply to Sam and say Friday works", True),
    ("How's my week looking?", True),
    ("Do it", False),
    ("Prep me for my meeting with Dana", True),
    ("I'm heading to Palo Alto after this, find me a Caltrain after 6 and a boba place on the way", True),
    ("Add it", False),
]


def record_story(cfg, tools: Tools, cache: DemoCache, phrases: list[tuple[str, bool]] | None = None, say: Callable[[str], None] = print) -> dict:
    """Run the story phrases through `tools` (live) with writes switched off, saving every read to `cache`.

    Uses a throw-away state directory, so it never touches your real memory. Returns a summary dict."""
    import tempfile

    from chief.intent import handle_utterance
    from chief.runtime import Runtime

    recorder = RecorderTools(tools, cache, allow_writes=False)
    summary = {"phrases": 0, "skipped": 0, "errors": [], "entries": 0}
    with tempfile.TemporaryDirectory(prefix="chief-record-") as tmp:
        scratch = type(cfg)(**{**cfg.__dict__, "state_dir": Path(tmp)})
        rt = Runtime(scratch, tools=recorder, notify=lambda p: None)
        for phrase, runs in phrases or STORY:
            if not runs:
                summary["skipped"] += 1
                say(f"  skip   {phrase!r}  (approval step: never run while recording)")
                continue
            before = len(recorder.recorded)
            try:
                out = handle_utterance(rt, phrase)
            except Exception as exc:
                summary["errors"].append(f"{phrase}: {type(exc).__name__}: {exc}")
                say(f"  FAIL   {phrase!r}: {type(exc).__name__}: {exc}")
                continue
            summary["phrases"] += 1
            say(f"  ok     {phrase!r}  ->  {len(recorder.recorded) - before} read(s) recorded; says: {out['say'][:70]!r}")
    for name, why in recorder.failed:
        summary["errors"].append(f"{name}: {why}")
    summary["entries"] = len(cache)
    return summary

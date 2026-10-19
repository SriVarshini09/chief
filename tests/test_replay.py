from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from chief.activity import ActivityBus
from chief.config import Config
from chief.intent import handle_utterance
from chief.models import Email
from chief.runtime import Runtime
from chief.tools import get_tools
from chief.tools.base import ALL_TOOLS
from chief.tools.mock import MockTools
from chief.tools.recording import RecordingTools
from chief.tools.replay import (
    STORY,
    CacheMiss,
    DemoCache,
    FailoverTools,
    RecorderTools,
    ReplayTools,
    WriteNotAllowed,
    record_story,
    redact,
)

NOW = datetime(2026, 10, 21, 8, 0)


class DeadLive(MockTools):
    """Live tools whose network is gone: every call fails."""

    def __init__(self):
        super().__init__(NOW)
        self.attempts: list[str] = []

    def _boom(self, name):
        self.attempts.append(name)
        raise ConnectionError("network is unreachable")


for _n in ALL_TOOLS:
    setattr(DeadLive, _n, (lambda n: lambda self, *a, **k: self._boom(n))(_n))


class SlowLive(MockTools):
    def list_events(self, start, end):
        time.sleep(1.0)
        return super().list_events(start, end)


@pytest.fixture
def cache(tmp_path):
    return DemoCache(tmp_path / "demo_cache.json")


@pytest.fixture
def recorded(tmp_path, cache):
    """A cache filled by running the story through mock tools standing in for the live ones."""
    cfg = Config(mode="live", state_dir=tmp_path / "s", now=NOW)
    mock = MockTools(NOW)
    summary = record_story(cfg, mock, cache, say=lambda s: None)
    return cache, mock, summary


# ---------------------------------------------------------------- recorder / cache file


def test_recording_fills_the_cache_without_any_write(recorded):
    cache, mock, summary = recorded
    assert summary["errors"] == [] and summary["entries"] == len(cache) > 8
    assert set(cache.counts()) == {"list_events", "fetch_emails", "fetch_thread", "search_web", "find_free_slots", "read_health"}
    assert mock.calls == []  # not even a draft: recording never writes
    assert summary["skipped"] == 2  # "Do it" and "Add it" are never run


def test_cache_file_round_trips_and_keeps_only_what_skills_use(tmp_path, recorded):
    cache, _, _ = recorded
    cache.save()
    raw = json.loads(cache.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["recorded_at"]
    assert not list(tmp_path.glob("*.tmp"))  # atomic write cleaned up
    mails = next(e for e in raw["entries"].values() if e["tool"] == "fetch_emails")["result"]
    assert set(mails[0]) == {"id", "thread_id", "sender", "subject", "snippet", "received", "unread"}
    events = next(e for e in raw["entries"].values() if e["tool"] == "list_events")["result"]
    assert set(events[0]) == {"id", "title", "start", "end", "attendees"}  # no descriptions
    again = DemoCache.load(cache.path)
    assert len(again) == len(cache) and again.age(datetime.now() + timedelta(hours=2)) > timedelta(hours=1)


def test_secrets_are_redacted_and_text_is_clipped(cache):
    leaky = Email("m1", "t1", "a@b.example", "Zoom", "join https://x.example/j/1?pwd=hunter2abc and key sk-ABCDEFGHIJKLMNOPQRSTUV " + "x" * 5000, NOW)

    class T(MockTools):
        def fetch_emails(self, query="", max_results=10):
            return [leaky]

    RecorderTools(T(NOW), cache).fetch_emails("", 5)
    blob = json.dumps(cache.entries)
    assert "hunter2abc" not in blob and "sk-ABCDEFGHIJKLMNOPQRSTUV" not in blob
    assert len(blob) < 2000
    assert redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz") == "Authorization: Bearer REDACTED"


def test_writes_are_not_cached_and_blocked_while_recording(cache):
    mock = MockTools(NOW)
    rec = RecorderTools(mock, cache, allow_writes=False)
    with pytest.raises(WriteNotAllowed):
        rec.patch_event("ev-1", {"summary": "x"})
    with pytest.raises(WriteNotAllowed):
        rec.create_draft("a@b.c", "s", "b")
    assert mock.calls == [] and len(cache) == 0


def test_a_failed_recording_does_not_overwrite_a_good_cache(tmp_path):
    from chief import launcher

    path = tmp_path / "demo_cache.json"
    good = '{"version": 1, "recorded_at": "2026-01-01T00:00:00", "entries": {}}'
    path.write_text(good, encoding="utf-8")
    cfg = Config(mode="live", state_dir=tmp_path / "s", now=NOW, cache_file=path)
    lines: list[str] = []
    assert launcher.record_demo_main(cfg, tools=DeadLive(), out=lines.append) == 1
    assert "Nothing was recorded" in "\n".join(lines)
    assert path.read_text(encoding="utf-8") == good


# ---------------------------------------------------------------- replay


def test_replay_serves_the_recorded_answer_flagged_cached(recorded):
    cache, mock, _ = recorded
    replay = ReplayTools(cache)
    got = replay.list_events(NOW - timedelta(days=14), NOW)
    assert [e.id for e in got] == [e.id for e in mock.list_events(NOW - timedelta(days=14), NOW)]
    meta = replay.pop_meta()
    assert meta["cached"] is True and meta["cache_match"] == "exact"
    assert replay.pop_meta() == {}  # read once


def test_replay_tolerates_the_demo_running_at_a_different_time(recorded):
    cache, mock, _ = recorded
    later = NOW + timedelta(minutes=7)
    replay = ReplayTools(cache)
    got = replay.list_events(later, later + timedelta(days=7))
    assert got and {e.id for e in got} <= {e.id for e in mock.list_events(NOW, NOW + timedelta(days=8))}
    assert replay.pop_meta()["cache_match"] == "derived"
    assert replay.fetch_emails("sam", 5)[0].sender == "sam@example.com"
    hits = replay.search_web(
        "Find Caltrain departures after 6:07 PM on Thursday October 22 to Palo Alto. Use only official sources: caltrain.com and 511.org (transit.511.org). Quote the departure and arrival times exactly.",
        ["Caltrain schedule to Palo Alto after 6:07 PM site:caltrain.com", "511 Caltrain trip to Palo Alto site:511.org"],
        5,
    )
    assert hits and "Caltrain" in hits[0]["title"]


def test_replay_miss_says_how_to_fix_it(cache):
    with pytest.raises(CacheMiss, match="record-demo"):
        ReplayTools(cache).fetch_thread("nope")


def test_writes_are_never_replayed(recorded):
    cache, _, _ = recorded
    replay = ReplayTools(cache)
    calls = (
        lambda: replay.patch_event("ev-1", {"summary": "x"}),
        lambda: replay.create_event("t", "2026-10-22T10:00:00", "2026-10-22T11:00:00"),
        lambda: replay.send_email("a@b.c", "s", "b"),
        lambda: replay.create_draft("a@b.c", "s", "b"),
        lambda: replay.delete_event("e"),
    )
    for call in calls:
        with pytest.raises(WriteNotAllowed, match="NOT done"):
            call()


def test_offline_mode_simulates_writes_and_says_so(cache):
    mock = MockTools(NOW)
    replay = ReplayTools(cache, writes=mock)
    out = replay.create_event("Caltrain to Palo Alto", "2026-10-21T18:30:00", "2026-10-21T19:15:00")
    assert out["simulated"] is True and replay.pop_meta() == {"simulated": True}
    out = replay.patch_event("a-real-google-id-the-mock-never-saw", {"summary": "x"})
    assert out["simulated"] is True  # no KeyError for ids the mock does not know
    assert [n for n, _ in replay.simulated] == ["create_event", "patch_event"]


def test_offline_calendar_shows_the_simulated_changes(recorded):
    cache, _, _ = recorded
    replay = ReplayTools(cache, writes=MockTools(NOW))
    day = NOW + timedelta(days=1)
    before = replay.list_events(day.replace(hour=0), day.replace(hour=23))
    target = next(e for e in before if "Weekly status" in e.title)
    replay.patch_event(target.id, {"summary": "[Async] Weekly status"})
    replay.create_event("Focus block (protected by Chief)", day.replace(hour=14).isoformat(), day.replace(hour=15, minute=30).isoformat())
    after = replay.list_events(day.replace(hour=0), day.replace(hour=23))
    assert any(e.title == "[Async] Weekly status" for e in after) and any(e.title.startswith("Focus block") for e in after)
    assert len(after) == len(before) + 1
    assert replay.pop_meta()["cached"] is True  # still labelled: this is a rehearsal, not your calendar


# ---------------------------------------------------------------- failover


def test_live_failure_serves_cache_and_marks_the_event(recorded):
    cache, mock, _ = recorded
    fo = FailoverTools(DeadLive(), ReplayTools(cache), timeout=1)
    bus = ActivityBus()
    tools = RecordingTools(fo, bus)
    got = tools.fetch_emails("", 10)
    assert [m.id for m in got] == [m.id for m in mock.fetch_emails("", 10)]
    done = [e for e in bus.since(0) if e.kind == "read"][0]
    assert done.detail["cached"] is True and done.title.endswith("(cached)") and done.ok
    assert "ConnectionError" in done.detail["cache_reason"]


def test_live_success_is_not_marked_cached(recorded):
    cache, _, _ = recorded
    bus = ActivityBus()
    tools = RecordingTools(FailoverTools(MockTools(NOW), ReplayTools(cache), timeout=1), bus)
    tools.fetch_emails("", 10)
    done = [e for e in bus.since(0) if e.kind == "read"][0]
    assert "cached" not in done.detail and "(cached)" not in done.title


def test_a_slow_live_call_times_out_to_the_cache(recorded):
    cache, _, _ = recorded
    fo = FailoverTools(SlowLive(NOW), ReplayTools(cache), timeout=0.1)
    started = time.monotonic()
    got = fo.list_events(NOW - timedelta(days=14), NOW)
    assert got and time.monotonic() - started < 0.8
    meta = fo.pop_meta()
    assert meta["cached"] and "TimeoutError" in meta["cache_reason"]


def test_after_a_failure_reads_skip_live_until_the_breaker_resets(recorded):
    cache, _, _ = recorded
    clock = [0.0]
    live = DeadLive()
    fo = FailoverTools(live, ReplayTools(cache), timeout=1, breaker_seconds=30, clock=lambda: clock[0])
    fo.fetch_emails("", 10)
    fo.fetch_emails("", 10)
    assert live.attempts == ["fetch_emails"]  # the second read did not wait out another timeout
    assert not fo.live_ok
    clock[0] = 31
    fo.fetch_emails("", 10)
    assert live.attempts == ["fetch_emails", "fetch_emails"]  # it probes again


def test_failover_never_replays_or_fakes_a_write(recorded):
    cache, _, _ = recorded
    fo = FailoverTools(DeadLive(), ReplayTools(cache, writes=MockTools(NOW)), timeout=1)  # even with a mock around
    calls = (
        lambda: fo.patch_event("ev-1", {"summary": "x"}),
        lambda: fo.send_email("a@b.c", "s", "b"),
        lambda: fo.create_event("t", "2026-10-22T10:00:00", "2026-10-22T11:00:00"),
        lambda: fo.create_draft("a@b.c", "s", "b"),
    )
    for call in calls:
        with pytest.raises(RuntimeError, match="NOT done and was not faked"):
            call()
    assert fo.pop_meta() == {}


def test_a_failed_write_shows_as_an_error_in_the_stream(recorded):
    cache, _, _ = recorded
    bus = ActivityBus()
    tools = RecordingTools(FailoverTools(DeadLive(), ReplayTools(cache), timeout=1), bus)
    with pytest.raises(RuntimeError):
        tools.patch_event("ev-1", {"summary": "x"})
    err = [e for e in bus.since(0) if e.kind == "error"][0]
    assert not err.ok and "NOT done" in err.title


def test_a_write_goes_live_when_live_is_up(cache):
    mock = MockTools(NOW)
    fo = FailoverTools(mock, ReplayTools(cache), timeout=1)
    assert fo.create_event("t", "2026-10-22T10:00:00", "2026-10-22T11:00:00")["id"]
    assert [c[0] for c in mock.calls] == ["create_event"]


def test_a_read_with_no_cache_fails_with_both_reasons(cache):
    fo = FailoverTools(DeadLive(), ReplayTools(cache), timeout=1)
    with pytest.raises(RuntimeError, match="network is unreachable.*no answer"):
        fo.fetch_emails("", 3)


# ---------------------------------------------------------------- wiring + the story end to end


def test_get_tools_wires_the_modes(tmp_path):
    path = tmp_path / "c.json"
    DemoCache(path).save()
    base = dict(state_dir=tmp_path / "s", cache_file=path, now=NOW)
    assert isinstance(get_tools(Config(mode="mock", **base)), MockTools)
    fo = get_tools(Config(mode="demo", live_timeout=2.5, **base))
    assert isinstance(fo, FailoverTools) and fo.timeout == 2.5 and fo.replay.writes is None
    off = get_tools(Config(mode="demo-offline", **base))
    assert isinstance(off, ReplayTools) and isinstance(off.writes, MockTools)


def test_the_story_survives_a_dead_network_in_offline_mode(tmp_path, recorded):
    cache, _, _ = recorded
    cache.save()
    cfg = Config(mode="demo-offline", state_dir=tmp_path / "off", cache_file=cache.path, now=NOW + timedelta(minutes=5))
    rt = Runtime(cfg)  # get_tools builds ReplayTools + simulated writes from the file
    says = [handle_utterance(rt, p)["say"] for p, _ in STORY]
    assert says[0].startswith("Inbox:") and "draft reply to Sam" in says[1] and "slept under 6 hours" in says[2]
    assert says[3].startswith("Done. 4 change(s) made.") and says[4].startswith("Prep for Acme") and says[5].startswith("Caltrain to Palo Alto")
    events = rt.bus.since(0)
    reads = [e for e in events if e.kind == "read"]
    assert reads and all(e.detail.get("cached") for e in reads)
    writes = [e for e in events if e.kind == "write"]
    assert writes and all(e.detail.get("simulated") and e.title.endswith("(simulated)") for e in writes)


def test_failover_story_with_live_down_reads_from_cache_and_refuses_writes(tmp_path, recorded):
    cache, _, _ = recorded
    tools = FailoverTools(DeadLive(), ReplayTools(cache), timeout=1)
    rt = Runtime(Config(mode="demo", state_dir=tmp_path / "fo", now=NOW), tools=tools)
    inbox = handle_utterance(rt, "What's in my inbox?")
    assert inbox["say"].startswith("Inbox:")
    week = handle_utterance(rt, "How's my week looking?")
    assert week["proposal"] and week["proposal"].actions
    done = handle_utterance(rt, "do it")
    assert done["say"].startswith("That didn't go through")  # honest: live is down, so nothing was done
    assert any(e.kind == "error" and "NOT done" in e.title for e in rt.bus.since(0))
    assert not [e for e in rt.bus.since(0) if e.kind == "write"]


# ---------------------------------------------------------------- local tools never use the cache or the breaker


class LocalLive(MockTools):
    def list_folder(self, path):
        if path == "forbidden":
            raise PermissionError("outside the allowed folders")
        return {"path": path, "entries": [{"name": "plan.pdf"}]}

    def fetch_emails(self, query="", max_results=10):
        raise ConnectionError("down")


def test_local_tools_bypass_cache_flags_and_breaker(cache):
    fo = FailoverTools(LocalLive(NOW), ReplayTools(cache), timeout=1)
    assert fo.list_folder("docs")["entries"][0]["name"] == "plan.pdf" and fo.pop_meta() == {}
    with pytest.raises(PermissionError, match="outside"):  # the real error, not wrapped
        fo.list_folder("forbidden")
    assert fo.live_ok  # a refused folder does not push calendar/email reads onto the cache


def test_recording_never_caches_folder_listings(cache):
    rec = RecorderTools(LocalLive(NOW), cache, allow_writes=False)
    assert rec.list_folder("docs")["entries"]
    assert len(cache) == 0


def test_offline_folder_listing_uses_the_local_backend_unflagged(cache):
    replay = ReplayTools(cache, writes=LocalLive(NOW))
    assert replay.list_folder("docs")["entries"] and replay.pop_meta() == {}
    with pytest.raises(RuntimeError, match="local tools"):
        ReplayTools(cache).list_folder("docs")

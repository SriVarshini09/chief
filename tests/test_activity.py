"""The activity stream: what the Stage screen and the real-window layer both follow."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from chief.activity import ActivityBus
from chief.intent import handle_utterance
from chief.tools.recording import RecordingTools


def kinds(rt, since=0):
    return [e.kind for e in rt.bus.since(since)]


def test_bus_orders_events_and_supports_since():
    bus = ActivityBus()
    a, b = bus.emit("step", "one"), bus.emit("step", "two")
    assert (a.seq, b.seq) == (1, 2)
    assert [e.title for e in bus.since(1)] == ["two"]


def test_bus_is_bounded_and_survives_a_broken_subscriber():
    bus = ActivityBus(limit=3)
    bus.subscribe(lambda e: 1 / 0)
    for i in range(5):
        bus.emit("step", str(i))
    assert [e.title for e in bus.since(0)] == ["2", "3", "4"]


def test_reads_report_working_then_result_with_a_shared_ref(tools):
    bus = ActivityBus()
    rec = RecordingTools(tools, bus)
    rec.list_events(tools.now, tools.now + timedelta(days=2))
    working, result = bus.since(0)
    assert (working.kind, result.kind) == ("working", "read")
    assert working.detail["ref"] == result.detail["ref"]
    assert result.tool == "list_events"
    assert result.detail["result"] and "title" in result.detail["result"][0]


def test_email_detail_has_snippets_only(tools):
    bus = ActivityBus()
    RecordingTools(tools, bus).fetch_emails("", 10)
    detail = bus.since(0)[-1].detail["result"]
    assert detail and {"sender", "subject", "snippet"} <= set(detail[0])
    json.dumps(bus.since(0)[-1].to_json())  # everything on the bus is JSON-safe


def test_failures_are_reported_and_still_raise(tools, monkeypatch):
    bus = ActivityBus()
    monkeypatch.setattr(tools, "search_web", lambda **_: (_ for _ in ()).throw(RuntimeError("quota")))
    with pytest.raises(RuntimeError):
        RecordingTools(tools, bus).search_web(objective="x", queries=["x"])
    assert bus.since(0)[-1].kind == "error" and not bus.since(0)[-1].ok


def test_a_spoken_request_is_fully_narrated(rt):
    handle_utterance(rt, "how's my week looking?")
    ks = kinds(rt)
    assert ks[0] == "heard" and ks[-1] == "say"
    assert "step" in ks and "read" in ks and "proposal" in ks
    assert "write" not in ks  # nothing is changed before approval


def test_writes_appear_only_after_approval(rt, tools):
    handle_utterance(rt, "how's my week looking?")
    mark = rt.bus.last_seq
    handle_utterance(rt, "do it")
    after = rt.bus.since(mark)
    writes = [e for e in after if e.kind == "write"]
    assert [w.tool for w in writes] == ["patch_event", "patch_event", "patch_event", "create_event"]
    assert any(e.kind == "approved" for e in after)


def test_skipping_is_reported_and_runs_nothing(rt):
    handle_utterance(rt, "audit")
    handle_utterance(rt, "not now")
    assert "skipped" in kinds(rt) and "write" not in kinds(rt)


def test_a_subscriber_sees_events_live(rt):
    seen = []
    rt.bus.subscribe(lambda e: seen.append((e.kind, e.tool)))
    handle_utterance(rt, "what's in my inbox")
    assert ("read", "fetch_emails") in seen

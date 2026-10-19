"""The Stage screen: /stage, /stage/events and /stage/calendar."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient  # noqa: E402

from chief.intent import handle_utterance  # noqa: E402
from chief.web.app import create_app  # noqa: E402

TOKEN = "s3cret-test-token"
H = {"X-Chief-Token": TOKEN}


class FakeSTT:
    enabled = False

    def transcribe(self, audio, filename, content_type):  # pragma: no cover - never called here
        raise AssertionError


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt, TOKEN, transcriber=FakeSTT()))


def test_stage_routes_need_the_token(client):
    for path in ("/stage", "/stage/events", "/stage/calendar"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-Chief-Token": "nope"}).status_code == 401, path
        assert client.get(path, params={"token": TOKEN}).status_code == 200, path


def test_stage_page_is_served_and_polls(client):
    r = client.get("/stage", params={"token": TOKEN})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "/stage/events" in r.text and "/stage/calendar" in r.text
    assert "EventSource" not in r.text  # polling only: quick tunnels cannot carry SSE
    assert "http://" not in r.text.replace("http://www.w3.org", "") and "https://" not in r.text  # no CDN, no external calls


def test_events_since_returns_only_newer(client, rt):
    handle_utterance(rt, "what's in my inbox")
    everything = client.get("/stage/events", headers=H).json()
    seqs = [e["seq"] for e in everything["events"]]
    assert seqs == sorted(seqs) and everything["last_seq"] == seqs[-1] == rt.bus.last_seq
    newer = client.get("/stage/events", params={"since": seqs[1]}, headers=H).json()
    assert [e["seq"] for e in newer["events"]] == seqs[2:]
    assert client.get("/stage/events", params={"since": rt.bus.last_seq}, headers=H).json()["events"] == []


def test_events_show_reads_and_a_proposal(client, rt):
    handle_utterance(rt, "how's my week looking?")
    data = client.get("/stage/events", params={"since": 0}, headers=H).json()
    kinds = [e["kind"] for e in data["events"]]
    assert kinds[0] == "heard" and kinds[-1] == "say"
    assert "read" in kinds and "proposal" in kinds and "write" not in kinds
    reads = [e for e in data["events"] if e["kind"] == "read"]
    assert {"list_events", "read_health"} <= {e["tool"] for e in reads}
    working = {e["detail"]["ref"] for e in data["events"] if e["kind"] == "working"}
    assert {e["detail"]["ref"] for e in reads} <= working  # every result has a "working" twin to replace
    proposal = next(e for e in data["events"] if e["kind"] == "proposal")
    assert proposal["detail"]["actions"] and proposal["detail"]["id"] in {p["id"] for p in data["pending"]}


def test_inbox_events_carry_sender_subject_snippet(client, rt):
    handle_utterance(rt, "what's in my inbox")
    mail = next(e for e in client.get("/stage/events", headers=H).json()["events"] if e["tool"] == "fetch_emails" and e["kind"] == "read")
    assert {"sender", "subject", "snippet"} <= set(mail["detail"]["result"][0])


def test_calendar_shows_the_next_day_with_events_and_the_day_before(client, rt):
    cal = client.get("/stage/calendar", headers=H).json()
    assert [d["date"] for d in cal["days"]] == ["2026-10-20", "2026-10-21"]  # NOW is Wed 21 Oct, 8am
    assert cal["focus"] == "2026-10-21"
    wed = cal["days"][1]["events"]
    assert wed and all({"id", "title", "start", "end", "minutes"} <= set(e) for e in wed)
    assert [e["start"] for e in wed] == sorted(e["start"] for e in wed)


def test_calendar_can_be_pinned_to_a_day_or_an_event(client):
    thu = client.get("/stage/calendar", params={"day": "2026-10-22"}, headers=H).json()
    assert thu["focus"] == "2026-10-22" and len(thu["days"][1]["events"]) == 6
    by_event = client.get("/stage/calendar", params={"event": "ev-20261022-13"}, headers=H).json()
    assert by_event["focus"] == "2026-10-22"
    assert client.get("/stage/calendar", params={"day": "soon"}, headers=H).status_code == 400


def test_calendar_says_whether_the_day_was_pinned(client):
    assert client.get("/stage/calendar", headers=H).json()["pinned"] is False
    assert client.get("/stage/calendar", params={"day": "2026-10-22"}, headers=H).json()["pinned"] is True
    assert client.get("/stage/calendar", params={"event": "ev-20261022-13"}, headers=H).json()["pinned"] is True
    unknown = client.get("/stage/calendar", params={"event": "no-such-event"}, headers=H).json()
    assert unknown["pinned"] is False and unknown["focus"] == "2026-10-21"  # falls back to the next day with events


def test_events_pass_task_and_honesty_flags_through(client, rt):
    """Parallel tasks and cached/simulated flags are added to event detail elsewhere; the Stage only relays them."""
    rt.bus.emit("working", "Reading your email...", tool="fetch_emails", detail={"ref": 901, "task": "t1", "cached": True})
    rt.bus.emit("read", "Read your email: 0 message(s)", tool="fetch_emails", detail={"ref": 901, "task": "t1", "result": [], "simulated": True})
    events = client.get("/stage/events", headers=H).json()["events"]
    assert [e["detail"]["task"] for e in events] == ["t1", "t1"]
    assert events[0]["detail"]["cached"] is True and events[1]["detail"]["simulated"] is True


def test_events_report_the_mode_for_the_header_badge(client, rt):
    assert client.get("/stage/events", headers=H).json()["mode"] == rt.cfg.mode == "mock"


def test_stage_page_handles_lanes_tags_badge_and_several_proposals(client):
    html = client.get("/stage", params={"token": TOKEN}).text
    assert "detail.task" in html or "detailOf(e).task" in html  # lanes per task, optional
    assert "cached" in html and "simulated" in html  # honesty tags
    assert 'id="modeBadge"' in html and "CACHED" in html and "LIVE" in html and "MOCK" in html
    assert 'id="props"' in html  # one approval card per pending proposal


def test_reading_the_calendar_does_not_touch_the_activity_stream(client, rt, tools):
    before = rt.bus.last_seq
    client.get("/stage/calendar", headers=H)
    client.get("/stage/calendar", params={"day": "2026-10-22"}, headers=H)
    assert rt.bus.last_seq == before and tools.calls == []


def test_calendar_shows_the_focus_block_after_approval(client, rt):
    handle_utterance(rt, "how's my week looking?")
    day = {"day": "2026-10-22"}
    before = client.get("/stage/calendar", params=day, headers=H).json()["days"][1]["events"]
    assert not any("Focus block" in e["title"] for e in before)

    mark = rt.bus.last_seq
    handle_utterance(rt, "do it")
    after = client.get("/stage/calendar", params=day, headers=H).json()["days"][1]["events"]
    focus = [e for e in after if "Focus block" in e["title"]]
    assert len(focus) == 1 and focus[0]["minutes"] == 90
    assert sum(e["title"].startswith("[Async]") for e in after) == 2 and any(e["title"].startswith("Walk:") for e in after)

    news = client.get("/stage/events", params={"since": mark}, headers=H).json()
    kinds = [e["kind"] for e in news["events"]]
    assert "approved" in kinds and kinds.count("write") == 4 and news["pending"] == []

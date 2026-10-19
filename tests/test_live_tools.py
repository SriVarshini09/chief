"""LiveTools against fake Composio / Parallel / Slack clients. No network, no keys.

The fakes mimic only what was verified in the SDK source (execute(slug, arguments=) -> .data/.error/.result_type,
sessions.create(user_id=, toolkits=, tools=), search(...) -> .results). The *inner* shape of tool data is an
assumption (see chief/tools/live.py); these tests pin that assumption so a change is visible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from chief import health
from chief.config import Config
from chief.models import Proposal, ToolCall
from chief.runtime import Runtime
from chief.tools import ScopedTools
from chief.tools.composio_client import ComposioGateway, LiveToolError, connect_main
from chief.tools.live import READ_SLUGS, WRITE_SLUGS, LiveTools

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 21, 8, 0)


class FakeSession:
    def __init__(self, name, responses):
        self.name, self.responses, self.calls = name, responses, []

    def execute(self, slug, *, arguments=None):
        self.calls.append((slug, arguments))
        r = self.responses.get(slug, {})
        r = r(arguments) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        if isinstance(r, NS):
            return r
        return NS(data=r, error=None, result_type="completed")

    def authorize(self, toolkit):
        return NS(redirect_url=f"https://connect.example/{toolkit}")

    def toolkits(self, toolkits=None, **_):
        items = [NS(slug="gmail", connection=NS(is_active=True)), NS(slug="googlecalendar", connection=None)]
        return NS(items=[i for i in items if i.slug in (toolkits or [])])


class FakeComposio:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.created: list[dict] = []
        self.sessions = self
        self.made: list[FakeSession] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        s = FakeSession(len(self.created), self.responses)
        self.made.append(s)
        return s

    def executed(self):
        return [c for s in self.made for c in s.calls]


def make(responses=None, cfg=None, **kw):
    composio = FakeComposio(responses)
    gw = ComposioGateway(user_id="u1", composio=composio, read_tools=READ_SLUGS, write_tools=WRITE_SLUGS)
    return LiveTools(cfg or Config(), gateway=gw, tz=PDT, **kw), composio


def gcal(id, summary, start, end, **extra):
    return {"id": id, "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


EVENTS = {
    "GOOGLECALENDAR_EVENTS_LIST": {
        "items": [
            gcal("b", "Product review", "2026-10-22T10:00:00-07:00", "2026-10-22T11:00:00-07:00"),
            gcal("a", "Standup", "2026-10-22T16:00:00Z", "2026-10-22T16:30:00Z", attendees=[{"email": "t@x.io"}]),
            {"id": "d", "summary": "Holiday", "start": {"date": "2026-10-22"}, "end": {"date": "2026-10-23"}},
            gcal("c", "Cancelled", "2026-10-22T12:00:00-07:00", "2026-10-22T13:00:00-07:00", status="cancelled"),
            gcal("far", "Next week", "2026-10-29T09:00:00-07:00", "2026-10-29T10:00:00-07:00"),
        ]
    }
}


# --- calendar reads
def test_list_events_returns_naive_local_sorted_events():
    live, c = make(EVENTS)
    evs = live.list_events(datetime(2026, 10, 22), datetime(2026, 10, 23))
    assert [e.id for e in evs] == ["a", "b"]  # all-day, cancelled and out-of-window entries are dropped
    assert evs[0].start == datetime(2026, 10, 22, 9, 0) and evs[0].start.tzinfo is None  # 16:00Z -> 09:00 PDT
    assert evs[0].attendees == ["t@x.io"] and evs[1].minutes == 60
    slug, args = c.executed()[0]
    assert slug == "GOOGLECALENDAR_EVENTS_LIST"
    assert args["timeMin"] == "2026-10-22T00:00:00-07:00" and args["singleEvents"] is True
    assert "pageToken" not in args  # None values are not sent


def test_list_events_follows_pages():
    pages = iter([
        {"items": [gcal("a", "A", "2026-10-22T09:00:00-07:00", "2026-10-22T10:00:00-07:00")], "nextPageToken": "p2"},
        {"data": {"items": [gcal("b", "B", "2026-10-22T11:00:00-07:00", "2026-10-22T12:00:00-07:00")]}},  # nested
    ])
    live, c = make({"GOOGLECALENDAR_EVENTS_LIST": lambda a: next(pages)})
    assert [e.id for e in live.list_events(datetime(2026, 10, 22), datetime(2026, 10, 23))] == ["a", "b"]
    assert c.executed()[1][1]["pageToken"] == "p2"


def test_find_free_slots_matches_mock_rules():
    live, _ = make(EVENTS)
    day0, day1 = datetime(2026, 10, 22, 8), datetime(2026, 10, 22, 12)
    slots = live.find_free_slots(day0, day1, 30)
    assert slots == [(day0, datetime(2026, 10, 22, 9)), (datetime(2026, 10, 22, 9, 30), datetime(2026, 10, 22, 10)),
                     (datetime(2026, 10, 22, 11), day1)]
    # an [Async] event stops blocking time
    asyncy = {"GOOGLECALENDAR_EVENTS_LIST": {"items": [gcal("x", "[Async] Sync", "2026-10-22T09:00:00-07:00", "2026-10-22T10:00:00-07:00")]}}
    live2, _ = make(asyncy)
    assert live2.find_free_slots(day0, day1, 30) == [(day0, day1)]


# --- email
MAILS = {
    "GMAIL_FETCH_EMAILS": {
        "messages": [
            {"messageId": "m1", "threadId": "t1", "sender": "Sam <sam@x.io>", "subject": "Deck?",
             "messageText": "Can you send\n the deck?", "messageTimestamp": "2026-10-21T14:00:00Z", "labelIds": ["INBOX", "UNREAD"]},
            {"messageId": "m2", "threadId": "t2", "sender": "d@acme.io", "subject": "Pricing",
             "preview": {"body": "tiers"}, "internalDate": "1792000000000", "labelIds": ["INBOX"]},
        ]
    },
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": {
        "messages": [
            {"messageId": "m9", "sender": "me", "subject": "Re", "messageText": "ok", "messageTimestamp": "2026-10-21T15:00:00Z"},
            {"messageId": "m8", "sender": "sam", "subject": "Q", "messageText": "hi", "messageTimestamp": "2026-10-21T14:00:00Z"},
        ]
    },
}


def test_fetch_emails_maps_to_email_model():
    live, c = make(MAILS)
    mails = live.fetch_emails("is:unread", max_results=5)
    assert [m.id for m in mails] == ["m2", "m1"] or [m.id for m in mails] == ["m1", "m2"]
    by = {m.id: m for m in mails}
    assert by["m1"].snippet == "Can you send the deck?" and by["m1"].unread is True
    assert by["m1"].received == datetime(2026, 10, 21, 7, 0) and by["m1"].thread_id == "t1"
    assert by["m2"].unread is False and by["m2"].snippet == "tiers"
    assert mails == sorted(mails, key=lambda m: m.received, reverse=True)
    assert c.executed()[0] == ("GMAIL_FETCH_EMAILS", {"query": "is:unread", "max_results": 5, "verbose": True, "user_id": "me"})


def test_fetch_thread_oldest_first_and_fills_thread_id():
    live, _ = make(MAILS)
    thread = live.fetch_thread("t7")
    assert [m.id for m in thread] == ["m8", "m9"] and {m.thread_id for m in thread} == {"t7"}


def test_create_draft_uses_read_session_and_returns_draft_id():
    live, c = make({"GMAIL_CREATE_EMAIL_DRAFT": {"response_data": {"draft_id": "r-1"}}})
    assert live.create_draft("a@b.io", "Hi", "Body", thread_id="t1") == {"draft_id": "r-1"}
    assert c.executed() == [("GMAIL_CREATE_EMAIL_DRAFT", {"recipient_email": "a@b.io", "subject": "Hi", "body": "Body", "thread_id": "t1", "user_id": "me"})]
    assert len(c.created) == 1  # no write session was opened for a draft


# --- writes
def test_patch_event_maps_fields():
    live, c = make()
    out = live.patch_event("ev1", {"summary": "[Async] X", "description": "d", "start": datetime(2026, 10, 22, 9, 30)})
    assert out == {"id": "ev1", "updated": True}
    slug, args = c.executed()[0]
    assert slug == "GOOGLECALENDAR_PATCH_EVENT"
    assert args == {"event_id": "ev1", "calendar_id": "primary", "summary": "[Async] X", "description": "d",
                    "start_time": "2026-10-22T09:30:00-07:00"}


def test_patch_event_refuses_unknown_fields():
    live, c = make()
    with pytest.raises(ValueError):
        live.patch_event("ev1", {"attendees": ["x"]})
    assert c.executed() == []


def test_create_and_delete_event():
    live, c = make({"GOOGLECALENDAR_CREATE_EVENT": {"id": "new1"}})
    out = live.create_event("Focus", "2026-10-22T13:00:00", "2026-10-22T14:30:00", "deep work")
    assert out == {"id": "new1"}
    assert live.delete_event("ev1") == {"deleted": True}
    (s1, a1), (s2, a2) = c.executed()
    assert s1 == "GOOGLECALENDAR_CREATE_EVENT"
    assert a1["start_datetime"] == "2026-10-22T13:00:00-07:00" and a1["end_datetime"] == "2026-10-22T14:30:00-07:00"
    assert a1["summary"] == "Focus" and a1["send_updates"] == "none" and "timezone" not in a1
    assert (s2, a2) == ("GOOGLECALENDAR_DELETE_EVENT", {"event_id": "ev1", "calendar_id": "primary"})


def test_named_timezone_is_sent_when_configured():
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo("America/Los_Angeles")
    except Exception:
        pytest.skip("tzdata not installed")
    live, c = make()
    live.tz = zone
    live.create_event("Focus", "2026-10-22T13:00:00", "2026-10-22T14:00:00")
    assert c.executed()[0][1]["timezone"] == "America/Los_Angeles"


def test_send_email_plain_and_threaded():
    live, c = make()
    assert live.send_email("a@b.io", "Hi", "Body") == {"sent": True}
    assert live.send_email("a@b.io", "Re: Hi", "Thanks", thread_id="t1") == {"sent": True}
    assert c.executed() == [
        ("GMAIL_SEND_EMAIL", {"recipient_email": "a@b.io", "subject": "Hi", "body": "Body", "user_id": "me"}),
        ("GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "recipient_email": "a@b.io", "message_body": "Thanks", "user_id": "me"}),
    ]


# --- session separation and errors
def test_read_session_cannot_hold_write_slugs_and_write_session_is_lazy():
    live, c = make(EVENTS)
    live.list_events(datetime(2026, 10, 22), datetime(2026, 10, 23))
    assert len(c.created) == 1
    enabled = {s for t in c.created[0]["tools"].values() for s in t["enable"]}
    assert enabled and not enabled & {s for v in WRITE_SLUGS.values() for s in v}
    assert c.created[0]["user_id"] == "u1" and c.created[0]["toolkits"] == ["gmail", "googlecalendar"]
    live.delete_event("ev1")
    assert len(c.created) == 2
    assert {s for t in c.created[1]["tools"].values() for s in t["enable"]} == {s for v in WRITE_SLUGS.values() for s in v}


def test_gateway_refuses_a_write_slug_in_the_read_session():
    live, _ = make()
    with pytest.raises(LiveToolError):
        live.gateway.execute("read", "GMAIL_SEND_EMAIL", {})


def test_failed_tool_raises_with_slug():
    bad = NS(data=None, error="token expired", result_type="failed")
    live, _ = make({"GMAIL_FETCH_EMAILS": bad})
    with pytest.raises(LiveToolError, match="GMAIL_FETCH_EMAILS.*token expired"):
        live.fetch_emails()


def test_missing_composio_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    live = LiveTools(Config(), tz=PDT)
    with pytest.raises(LiveToolError, match="COMPOSIO_API_KEY"):
        live.list_events(NOW, NOW + timedelta(days=1))


# --- Parallel
class FakeParallel:
    def __init__(self):
        self.calls = []

    def search(self, **kw):
        self.calls.append(kw)
        return NS(results=[
            NS(title="Caltrain schedule", url="https://www.caltrain.com/s", excerpts=["8:05 San Francisco", "8:35"], publish_date=None),
            NS(title=None, url="https://511.org/x", excerpts=["x"], publish_date=None),
            NS(title="third", url="https://e.com", excerpts=[], publish_date=None),
        ])


def test_search_web_shape_and_parameters(monkeypatch):
    monkeypatch.delenv("CHIEF_SEARCH_DOMAINS", raising=False)
    p = FakeParallel()
    live, _ = make(search_client=p)
    out = live.search_web("next train", ["caltrain schedule", "san mateo to sf"], max_results=2)
    assert out == [
        {"title": "Caltrain schedule", "url": "https://www.caltrain.com/s", "excerpts": ["8:05 San Francisco", "8:35"]},
        {"title": "https://511.org/x", "url": "https://511.org/x", "excerpts": ["x"]},
    ]
    assert p.calls == [{"objective": "next train", "search_queries": ["caltrain schedule", "san mateo to sf"],
                        "mode": "fast", "advanced_settings": {"max_results": 2}}]


def test_search_web_pins_domains_from_argument_or_env(monkeypatch):
    p = FakeParallel()
    live, _ = make(search_client=p)
    live.search_web("t", ["q"], include_domains=["caltrain.com", "511.org"])
    assert p.calls[-1]["advanced_settings"]["source_policy"] == {"include_domains": ["caltrain.com", "511.org"]}
    monkeypatch.setenv("CHIEF_SEARCH_DOMAINS", "caltrain.com, 511.org")
    live.search_web("t", ["q"])
    assert p.calls[-1]["advanced_settings"]["source_policy"]["include_domains"] == ["caltrain.com", "511.org"]


def test_search_web_errors_and_missing_key(monkeypatch):
    class Boom:
        def search(self, **_):
            raise RuntimeError("429")

    live, _ = make(search_client=Boom())
    with pytest.raises(LiveToolError, match="429"):
        live.search_web("o", ["q"])
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    live2, _ = make()
    with pytest.raises(LiveToolError, match="PARALLEL_API_KEY"):
        live2.search_web("o", ["q"])


# --- health, slack
def test_read_health_from_csv_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIEF_HEALTH_CSV", raising=False)
    live = LiveTools(Config(data_dir=tmp_path), tz=PDT)
    with pytest.raises(LiveToolError, match="sleep CSV"):
        live.read_health()
    health.write_sample_csv(tmp_path / "sleep.csv", NOW, 14)
    assert len(live.read_health(days=14)) == 15 and len(live.read_health(days=3)) == 4


def test_post_slack(monkeypatch):
    sent = []
    client = NS(chat_postMessage=lambda **kw: sent.append(kw) or {"ok": True})
    monkeypatch.setenv("SLACK_CHANNEL", "C123")
    live, _ = make(slack_client=client)
    assert live.post_slack("hi") == {"ok": True} and sent == [{"channel": "C123", "text": "hi"}]


# --- approval gate integration: nothing is written until approve()
def test_live_writes_only_happen_after_approval(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIEF_HEALTH_CSV", raising=False)
    cfg = Config(mode="live", state_dir=tmp_path / "state", data_dir=tmp_path, now=NOW)
    health.write_sample_csv(tmp_path / "sleep.csv", NOW, 14)
    thursday = datetime(2026, 10, 22)
    titles = ["Standup", "Product review", "1:1 with Sam", "Design crit", "Vendor sync", "Weekly status"]
    items = []
    for offset in range(-14, 8):  # same shape as MockTools: Mon/Tue/Thu are heavy days
        day = NOW.date() + timedelta(days=offset)
        if day.weekday() in health.HEAVY_WEEKDAYS:
            for h, t in zip((9, 10, 11, 13, 14, 15), titles):
                items.append(gcal(f"e{day:%m%d}{h}", t, f"{day}T{h:02d}:00:00-07:00", f"{day}T{h + 1:02d}:00:00-07:00"))
    live, c = make({"GOOGLECALENDAR_EVENTS_LIST": {"items": items}}, cfg=cfg)
    # sanity: the fake calendar looks like a heavy day to the audit skill
    assert len(live.list_events(thursday, thursday + timedelta(days=1))) == 6
    cards: list[Proposal] = []
    rt = Runtime(cfg, tools=live, notify=cards.append)
    p = rt.run_skill("audit")
    assert p is not None
    writes = {"GOOGLECALENDAR_PATCH_EVENT", "GOOGLECALENDAR_CREATE_EVENT", "GOOGLECALENDAR_DELETE_EVENT", "GMAIL_SEND_EMAIL"}
    assert not {s for s, _ in c.executed()} & writes
    with pytest.raises(PermissionError):
        ScopedTools(live, {"patch_event"}, "audit").patch_event("x", {})
    results = rt.gate.approve(p.id)
    assert all("error" not in r for r in results), results
    assert {s for s, _ in c.executed()} & writes


# --- connect link
def test_connect_link_and_status(capsys):
    gw = ComposioGateway(user_id="u1", composio=FakeComposio())
    assert connect_main(["gmail"], gateway_factory=lambda: gw) == 0
    assert "https://connect.example/gmail" in capsys.readouterr().out
    assert connect_main(["calendar"], gateway_factory=lambda: gw) == 0
    assert "https://connect.example/googlecalendar" in capsys.readouterr().out
    assert connect_main(["--status"], gateway_factory=lambda: gw) == 0
    out = capsys.readouterr().out
    assert "gmail" in out and "NOT connected" in out


def test_connect_without_key_prints_error(monkeypatch, capsys):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    assert connect_main(["gmail"], gateway_factory=lambda: ComposioGateway()) == 1
    assert "COMPOSIO_API_KEY" in capsys.readouterr().out

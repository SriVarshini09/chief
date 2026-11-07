"""Approval cards in plain words. A card is what someone says yes to, so it has to be true and complete."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from chief.config import _hour  # noqa: E402
from chief.intent import handle_utterance  # noqa: E402
from chief.models import ToolCall, when  # noqa: E402
from chief.web.app import create_app  # noqa: E402

TOKEN = "t0ken/with space"
H = {"X-Chief-Token": TOKEN}


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt, TOKEN))


@pytest.mark.parametrize("title,expected", [
    ("[Async] Vendor sync", 'Mark "Vendor sync" as async'),
    ("Walk: 1:1 with Sam", 'Make "1:1 with Sam" a walking call'),
    ("Team lunch", 'Rename a calendar event to "Team lunch"'),
])
def test_calendar_changes_read_naturally(title, expected):
    plain = ToolCall("patch_event", {"event_id": "e1", "fields": {"summary": title, "description": "x"}}).plain()
    assert plain.startswith(expected) and "event_id" not in plain


def test_a_new_event_shows_its_title_and_times():
    plain = ToolCall("create_event", {"title": "Focus", "start": "2026-10-22T14:00:00", "end": "2026-10-22T15:30:00", "description": "d"}).plain()
    assert plain == 'Add "Focus" to your calendar: Thu Oct 22, 2:00 PM to 3:30 PM\nNotes, in full:\nd'


def test_an_event_across_two_days_shows_both_dates():
    plain = ToolCall("create_event", {"title": "Trip", "start": "2026-10-22T22:00:00", "end": "2026-10-23T07:00:00"}).plain()
    assert plain == 'Add "Trip" to your calendar: Thu Oct 22, 10:00 PM to Fri Oct 23, 7:00 AM'


def test_an_email_shows_everything_that_would_be_sent():
    action = ToolCall("send_email", {"to": "sam@example.com", "subject": "Re: Deck", "body": "Hi Sam,\n\nFriday works.\n\nBest", "thread_id": "t1"})
    plain = action.plain()
    assert "sam@example.com" in plain and "Re: Deck" in plain and "Hi Sam,\n\nFriday works.\n\nBest" in plain


def test_a_moved_event_shows_the_new_time():
    plain = ToolCall("patch_event", {"event_id": "e1", "fields": {"start": "2026-10-22T09:30:00"}}).plain()
    assert "start at Thu Oct 22, 9:30 AM" in plain


@pytest.mark.parametrize("action", [
    ToolCall("patch_event", {"event_id": "e1", "fields": {"attendees": ["x@evil.example"]}}),  # a field the wording would hide
    ToolCall("patch_event", {"event_id": "e1", "fields": {"summary": "Fine", "attendees": ["x@evil.example"]}}),
    ToolCall("patch_event", {"event_id": "e1", "fields": {"start": "whenever"}}),
    ToolCall("patch_event", {"id": "e1"}),
    ToolCall("send_email", {"to": "a@example.com", "subject": "s", "body": "b", "bcc": "x@evil.example"}),
    ToolCall("create_event", {"title": "T", "start": "soon", "end": "later"}),
    ToolCall("create_event", {"title": "T", "start": "2026-10-22T14:00:00", "end": "2026-10-22T15:00:00", "attendees": ["x"]}),
    ToolCall("open_file", {"path": "x"}),
])
def test_anything_not_fully_understood_shows_the_exact_call(action):
    assert action.plain() == action.describe()


def test_when_handles_noon_midnight_and_nonsense():
    assert when("2026-10-22T12:05:00") == "Thu Oct 22, 12:05 PM"
    assert when("2026-10-22T00:00:00") == "Thu Oct 22, 12:00 AM"
    assert when("nope") is None


def test_the_phone_card_has_plain_words_a_title_and_the_exact_calls(client):
    client.post("/ask", json={"text": "how's my week looking?"}, headers=H)
    card = client.get("/status", headers=H).json()["pending"][0]
    assert card["label"] == "Your week" and card["skill"] == "audit"
    assert len(card["plain"]) == len(card["actions"]) == 4
    assert card["plain"][0].startswith('Mark "') and card["actions"][0].startswith("patch_event(")


def test_an_unknown_request_is_answered_in_plain_words(rt):
    say = handle_utterance(rt, "what's the capital of France?")["say"]
    assert "your inbox" in say and "draft_reply" not in say and "AI model" in say


# ---- installing the phone page --------------------------------------------------------------------


def test_the_manifest_opens_the_app_with_the_token(client):
    r = client.get("/manifest.webmanifest", headers=H)
    data = r.json()
    assert r.headers["content-type"].startswith("application/manifest+json") and r.headers["cache-control"] == "no-store"
    assert data["display"] == "standalone" and data["start_url"] == "/?token=t0ken%2Fwith%20space"
    assert {i["sizes"] for i in data["icons"]} == {"192x192", "512x512"}


def test_every_icon_the_manifest_names_really_loads(client):
    for entry in client.get("/manifest.webmanifest", headers=H).json()["icons"]:
        r = client.get(entry["src"])
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


def test_install_routes_need_the_token(client):
    assert client.get("/manifest.webmanifest").status_code == 401
    assert client.get("/icons/icon-192.png").status_code == 401


@pytest.mark.parametrize("name", ["..%2Fapp.py", "app.py", "icon-1.png"])
def test_only_the_named_icons_are_served(client, name):
    assert client.get(f"/icons/{name}", headers=H).status_code == 404


@pytest.mark.parametrize("value,expected", [(None, 8), ("", 8), ("7", 7), ("0", 0), ("off", None), ("OFF", None), ("24", 8), ("soon", 8)])
def test_brief_hour_setting(value, expected):
    assert _hour(value, 8) == expected

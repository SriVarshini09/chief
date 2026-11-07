"""Launch security checks: the web token, guessing, Slack owners (see also tests/test_slack_app.py)."""

from __future__ import annotations

import pytest

from chief.config import token_problem


@pytest.mark.parametrize("token", ["demo123", "", "short", "my-fixed-token-1234567890", "aaaaaaaaaaaaaaaaaaaaaaaa", "chief-is-my-secret-word-ok"])
def test_weak_tokens_are_refused(token):
    assert token_problem(token) is not None


@pytest.mark.parametrize("token", ["Xk3fP9-qLm2Zr7Vw8Tn4Hs", "q8V_2nKd0Lw7Rt5Ym3Zx9Pa1"])
def test_random_tokens_are_fine(token):
    assert token_problem(token) is None


def test_chief_web_will_not_start_with_a_weak_token(monkeypatch, capsys):
    pytest.importorskip("fastapi")
    from chief.__main__ import _serve_web

    served = []
    monkeypatch.setenv("CHIEF_WEB_TOKEN", "demo123")
    monkeypatch.setattr("chief.web.app.serve", lambda *a: served.append(a))
    assert _serve_web(None, "127.0.0.1", 8765) == 2
    out = capsys.readouterr().out
    assert "Not starting" in out and "token_urlsafe" in out and served == []


def test_listening_on_the_network_warns(monkeypatch, capsys, rt):
    pytest.importorskip("fastapi")
    from chief.__main__ import _serve_web

    monkeypatch.setenv("CHIEF_WEB_TOKEN", "Xk3fP9-qLm2Zr7Vw8Tn4Hs")
    monkeypatch.setattr("chief.web.app.serve", lambda *a: None)
    monkeypatch.setattr("chief.telegram_app.start_with_web", lambda rt: False)
    assert _serve_web(rt, "0.0.0.0", 8765) == 0
    assert "WARNING: listening on 0.0.0.0" in capsys.readouterr().out


def test_guessing_the_token_gets_slowed_down(rt):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from chief.web.app import MAX_WRONG_TOKENS, create_app

    client = TestClient(create_app(rt, "Xk3fP9-qLm2Zr7Vw8Tn4Hs"))
    for i in range(MAX_WRONG_TOKENS):
        assert client.get("/status", headers={"X-Chief-Token": f"guess{i}"}).status_code == 401
    assert client.get("/status", headers={"X-Chief-Token": "guess-more"}).status_code == 429
    # even the right token waits: a guesser can't tell when they've hit it
    assert client.get("/status", headers={"X-Chief-Token": "Xk3fP9-qLm2Zr7Vw8Tn4Hs"}).status_code == 429


# ---- approval cards say everything the approval would do ----------------------------------------------


def test_a_card_names_the_event_it_changes_and_shows_new_notes_in_full(rt):
    from chief.models import Proposal, ToolCall

    standup = next(e for e in rt.tools.list_events(rt.now(), rt.now() + __import__("datetime").timedelta(days=2)) if e.title == "Standup")
    p = Proposal("calendar_edit", "x", [
        ToolCall("patch_event", {"event_id": standup.id, "fields": {"description": "Join here: https://evil.example/login"}}),
        ToolCall("delete_event", {"event_id": standup.id}),
    ], key="k1")
    rt.gate.submit(p)
    change, delete = rt.gate.plain(p)
    assert '[the event now: "Standup", Wed Oct 21, 10:00 AM]' in change and "https://evil.example/login" in change
    assert delete == 'Delete "Standup", Wed Oct 21, 10:00 AM from your calendar. Guests aren'"'"'t emailed: tell them yourself'


def test_the_telegram_card_shows_the_same_full_words(rt):
    from chief.telegram_app import card_text

    out = __import__("chief.intent", fromlist=["handle_utterance"]).handle_utterance(rt, "cancel the standup")
    text = card_text(out["proposal"], rt.gate.plain(out["proposal"]))
    assert 'Delete "Standup", Wed Oct 21, 10:00 AM from your calendar' in text


# ---- a plain "yes" only approves the card you were just shown -----------------------------------------

from chief.intent import handle_utterance as _say  # noqa: E402
from chief.tools.base import WRITE_TOOLS  # noqa: E402


def _writes(tools):
    return [c for c in tools.calls if c[0] in WRITE_TOOLS]


def test_yes_does_not_approve_a_card_you_were_never_shown(rt, tools):
    """A heartbeat card (or one on another screen) is never approved by a stray "sure"."""
    rt.run_skill("audit")  # the heartbeat made a card; this conversation never showed it
    out = _say(rt, "sure")
    assert out["say"].startswith("Approve what?") and "tap Approve" in out["say"] and _writes(tools) == []


def test_yes_after_a_question_still_means_the_card_you_saw(rt, tools):
    _say(rt, "how's my week looking")
    _say(rt, "what's in my inbox")
    assert "All done. I made" in _say(rt, "yes")["say"]


def test_an_old_card_needs_its_button(rt, tools):
    from datetime import timedelta

    p = _say(rt, "remind me to call mom at 6pm")["proposal"]
    rt.gate.since[p.id] -= timedelta(minutes=45)
    out = _say(rt, "yes")
    assert "waiting a while" in out["say"] and _writes(tools) == [] and p.id in rt.gate.pending


def test_undo_reverses_the_last_change_even_with_another_card_waiting(rt, tools):
    """Found in the fresh-install test: 'undo' skipped the waiting card instead of reversing what was just done."""
    reminder = _say(rt, "remind me to call mom at 6pm")["proposal"]
    _say(rt, "how's my week looking")
    done = _say(rt, "do it")
    assert "Say undo" in done["say"] and reminder.id in rt.gate.pending
    out = _say(rt, "undo")
    assert "reversed" in out["say"].lower() or "undid" in out["say"].lower() or "undone" in out["say"].lower(), out["say"]
    assert reminder.id in rt.gate.pending  # the other card is untouched


# ---- the token leaves the address bar; a reload still works -------------------------------------------

STRONG = "Xk3fP9-qLm2Zr7Vw8Tn4Hs"


@pytest.fixture
def web(rt):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from chief.web.app import create_app

    return TestClient(create_app(rt, STRONG))


def test_opening_the_link_sets_a_locked_down_cookie(web):
    r = web.get("/dashboard", params={"token": STRONG})
    cookie = r.headers["set-cookie"].lower()
    assert "chief_token=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert web.get("/dashboard").status_code == 200  # a reload, token no longer in the address


def test_the_cookie_alone_cannot_change_anything(web):
    web.get("/", params={"token": STRONG})
    assert web.post("/ask", json={"text": "do it"}).status_code == 401
    assert web.post("/location", json={"lat": 1.0, "lon": 1.0}).status_code == 401
    assert web.delete("/location").status_code == 401


def test_a_wrong_token_sets_no_cookie(web):
    r = web.get("/dashboard", params={"token": "Wrong-but-long-enough-1"})
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_every_answer_has_the_safety_headers(web):
    for r in (web.get("/status"), web.get("/status", headers={"X-Chief-Token": STRONG})):
        assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_pages_take_the_token_out_of_the_address_bar():
    from pathlib import Path

    for page in ("index.html", "dashboard.html", "stage.html"):
        text = (Path("chief/web") / page).read_text("utf-8")
        assert "history.replaceState" in text and 'sessionStorage.setItem("chief-token"' in text, page


# ---- the diary on disk keeps no mail and stays small --------------------------------------------------


def test_inbox_answers_are_not_written_to_disk(rt, cfg):
    _say(rt, "what's in my inbox")
    _say(rt, "what did Dana say?")
    saved = "".join(p.read_text("utf-8") for p in cfg.state_dir.rglob("*") if p.is_file())
    assert "pricing" not in saved.lower() and "not saved" in saved


def test_the_diary_rotates(rt, cfg, monkeypatch):
    monkeypatch.setattr(type(rt.memory), "LOG_LIMIT", 500)
    for i in range(40):
        rt.memory.log("note", n=i, text="x" * 40)
    assert (cfg.state_dir / "activity.1.jsonl").exists()
    assert (cfg.state_dir / "activity.jsonl").stat().st_size < 1200


# ---- small things found in the fresh-install test -----------------------------------------------------


def test_text_that_is_not_a_string_is_refused(web):
    h = {"X-Chief-Token": STRONG}
    for body in ({"text": None}, {"text": 42}, {"text": ["hi"]}):
        assert web.post("/ask", json=body, headers=h).status_code == 400
    assert web.get("/status", headers=h).json()["heard"] != "None"


def test_emoji_only_gets_a_kind_answer(rt):
    assert _say(rt, "\U0001F389\U0001F389")["say"].startswith("I can only read words")


def test_the_evening_greeting_does_not_mention_a_finished_meeting():
    from chief.dashboard import opening

    meetings = [{"title": "Acme pricing review", "start": 16.5, "end": 17.5, "clock": "4:30 PM"}]
    mail = {"reply": [], "deadline": [], "fyi": 0, "flagged": 0}
    assert "is done" in opening(18.0, meetings, mail, [], []) and "starting with" not in opening(18.0, meetings, mail, [], [])
    two = meetings + [{"title": "Dinner", "start": 19.0, "end": 20.0, "clock": "7:00 PM"}]
    assert "1 meeting left today. Next is Dinner at 7:00 PM." in opening(18.0, two, mail, [], [])
    assert "starting with Acme pricing review" in opening(9.0, meetings, mail, [], [])


def test_the_brief_after_the_meetings_says_so(rt, tools):
    from datetime import datetime, timedelta

    from chief.models import Event

    tools.events = {"e1": Event("e1", "Dentist", datetime(2026, 10, 21, 7, 0), datetime(2026, 10, 21, 7, 30))}
    assert "1 meeting today, all done. The last ended at 7:30 AM." in _say(rt, "morning brief")["say"]

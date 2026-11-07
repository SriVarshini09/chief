"""The morning brief: plain code, read-only, and safe to repeat."""

from __future__ import annotations

from datetime import datetime, timedelta

from chief.intent import handle_utterance
from chief.models import Email, Event
from chief.skills import load_skills
from chief.tools.base import WRITE_TOOLS


def test_the_brief_covers_the_day_and_the_mail(rt):
    say = handle_utterance(rt, "morning brief")["say"]
    assert say.startswith("Good morning. Here's your Wednesday.")
    assert "3 meetings. First: Standup at 10:00 AM. Done by 5:30 PM." in say
    assert "To answer: Sam (Deck for Friday?, by friday); Dana (Pricing follow-up)" in say
    assert "Deadlines: Billing (Invoice 1042, due oct 30)" in say


def test_it_reports_the_injection_email_without_repeating_it(rt):
    say = handle_utterance(rt, "morning brief")["say"]
    assert "I ignored 1 email that tried to give me orders." in say
    assert "attacker" not in say and "forward" not in say.lower()


def test_it_changes_nothing_and_leaves_nothing_to_approve(rt, tools):
    out = handle_utterance(rt, "what's my day")
    assert not out["proposal"].actions and not rt.gate.pending
    assert not [c for c in tools.calls if c[0] in WRITE_TOOLS]


def test_it_can_be_asked_for_twice(rt):
    assert "Good morning" in handle_utterance(rt, "morning brief")["say"]
    assert "Good morning" in handle_utterance(rt, "morning brief")["say"]


def test_it_works_without_a_model(rt):
    assert not rt.llm.enabled
    assert handle_utterance(rt, "morning brief")["steps"]


def test_it_reads_only_what_it_needs_and_stays_off_the_heartbeat():
    skill = load_skills()["morning_brief"]
    assert skill.allowed_tools == {"list_events", "fetch_emails"} and skill.propose_tools == set()
    assert skill.heartbeat is False


def test_a_quiet_day(rt, tools):
    tools.list_events = lambda start, end: []
    tools.fetch_emails = lambda *a, **k: []
    assert handle_utterance(rt, "morning brief")["say"] == "Good morning. Here's your Wednesday.\nNo meetings today."


def test_one_meeting_is_not_plural_and_all_day_events_do_not_count(rt, tools):
    day = datetime(2026, 10, 21)
    tools.list_events = lambda start, end: [
        Event("a", "Holiday", day, day + timedelta(days=1)),
        Event("b", "Dentist", day.replace(hour=14), day.replace(hour=15)),
    ]
    assert "1 meeting. First: Dentist at 2:00 PM. Done by 3:00 PM." in handle_utterance(rt, "morning brief")["say"]


def test_the_greeting_follows_the_clock(rt):
    rt.cfg.now = datetime(2026, 10, 21, 14, 40)
    assert handle_utterance(rt, "what's my day")["say"].startswith("Good afternoon. Here's your Wednesday.")


def test_old_or_read_mail_is_left_out(rt, tools):
    old = Email("1", "t", "sam@example.com", "Ancient", "Can you reply?", datetime(2026, 10, 1))
    seen = Email("2", "t", "sam@example.com", "Seen", "Can you reply?", datetime(2026, 10, 21, 7), unread=False)
    tools.fetch_emails = lambda *a, **k: [old, seen]
    say = handle_utterance(rt, "morning brief")["say"]
    assert "Ancient" not in say and "Seen" not in say

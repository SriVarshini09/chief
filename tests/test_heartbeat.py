from __future__ import annotations

from datetime import datetime


def test_tick_posts_one_card(rt, cards):
    proposals = rt.tick()
    assert len(proposals) == 1 and len(cards) == 1


def test_second_tick_is_silent(rt, cards):
    rt.tick()
    assert rt.tick() == []  # same situation, already pending
    assert len(cards) == 1


def test_no_checkins_outside_active_hours(rt, cards):
    assert rt.tick(datetime(2026, 10, 21, 3, 0)) == []
    assert cards == []


def test_skills_are_discovered(rt):
    assert "audit" in rt.skills
    assert rt.skills["audit"].propose_tools == {"patch_event", "create_event"}

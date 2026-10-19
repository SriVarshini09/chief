from __future__ import annotations

from datetime import datetime, timedelta

from chief.models import SleepDay
from chief.runtime import Runtime


def test_audit_finds_pattern_and_proposes_fix(rt):
    p = rt.run_skill("audit")
    assert p is not None
    assert p.key == "audit:2026-10-22"  # the upcoming Thursday
    tools_used = [a.tool for a in p.actions]
    assert tools_used.count("patch_event") == 3  # two async + one walking call
    assert tools_used.count("create_event") == 1  # the focus block
    assert "Thursday" in p.summary and "6 meetings" in p.summary


def test_approve_changes_the_calendar(rt, tools):
    p = rt.run_skill("audit")
    results = rt.gate.approve(p.id)
    assert all("error" not in r for r in results)
    titles = [e.title for e in tools.events.values()]
    assert any(t.startswith("[Async]") for t in titles)
    assert any(t.startswith("Walk:") for t in titles)
    assert any("Focus block" in t for t in titles)
    assert rt.memory.decided(p.key)


def test_same_situation_proposed_once(rt):
    assert rt.run_skill("audit") is not None
    assert rt.run_skill("audit") is None  # already pending


def test_stays_silent_when_sleep_is_fine(cfg, tools, cards):
    tools.read_health = lambda days=14: [SleepDay(cfg.now.date() - timedelta(days=i), 7.5) for i in range(15)]
    rt = Runtime(cfg, tools=tools, notify=cards.append)
    assert rt.run_skill("audit") is None
    assert cards == []


def test_stays_silent_without_enough_evidence(cfg, tools, cards):
    tools.read_health = lambda days=14: []
    rt = Runtime(cfg, tools=tools, notify=cards.append)
    assert rt.run_skill("audit") is None

"""After an approval: failures retry only what is left, email can be stopped, calendar changes can be undone."""

from __future__ import annotations

import pytest

from chief.config import Config
from chief.gate import results_text
from chief.intent import handle_utterance
from chief.models import Proposal, ToolCall
from chief.runtime import Runtime
from chief.tools.base import WRITE_TOOLS


class FakeTimer:
    """Stands in for threading.Timer: the test decides when the time is up."""

    made: list = []

    def __init__(self, seconds, fire, args):
        self.seconds, self.fire, self.args, self.cancelled, self.daemon = seconds, fire, args, False, False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def run_out(self):
        self.fire(*self.args)


@pytest.fixture
def held(tmp_path, tools, cards, cfg):
    FakeTimer.made = []
    cfg.send_delay = 30
    r = Runtime(cfg, tools=tools, notify=cards.append)
    r.gate.timer = FakeTimer
    return r


def writes(tools):
    return [c for c in tools.calls if c[0] in WRITE_TOOLS]


def reply(rt):
    return handle_utterance(rt, "reply to Sam and say Friday works")


# ---- a failure part way through ------------------------------------------------------------------


def test_a_retry_never_repeats_finished_steps(rt, tools, monkeypatch):
    rt.run_skill("audit")
    pid = next(iter(rt.gate.pending))
    real = tools.create_event
    monkeypatch.setattr(tools, "create_event", lambda **k: 1 / 0)
    assert "error" in rt.gate.approve(pid)[-1]
    assert len(writes(tools)) == 3  # the three calendar edits went through

    waiting = rt.gate.pending[pid]
    assert [a.tool for a in waiting.actions] == ["create_event"] and "3 of 4" in waiting.summary
    monkeypatch.setattr(tools, "create_event", real)
    assert "error" not in rt.gate.approve(pid)[-1]
    assert len(writes(tools)) == 4 and rt.memory.decided(waiting.key)


def test_a_failed_step_is_shown_again_and_not_proposed_twice(rt, tools, cards, monkeypatch):
    rt.run_skill("audit")
    pid = next(iter(rt.gate.pending))
    monkeypatch.setattr(tools, "patch_event", lambda *a, **k: 1 / 0)
    rt.gate.approve(pid)
    assert len(cards) == 2 and cards[1].id == pid
    rt.tick()
    assert len(rt.gate.pending) == 1  # the heartbeat does not add a second card


def test_results_text():
    assert results_text([{"tool": "x", "result": {}}]) == "Done. 1 change(s) made."
    assert "Sending in 30 seconds" in results_text([{"tool": "send_email", "result": {"held": True, "seconds": 30}}])
    both = results_text([{"tool": "a", "result": {}}, {"tool": "b", "error": "offline"}])
    assert "offline" in both and "1 change(s) went through" in both


# ---- stopping an email ---------------------------------------------------------------------------


def test_an_approved_email_waits_then_goes(held, tools):
    reply(held)
    out = handle_utterance(held, "do it")
    assert "Sending in 30 seconds" in out["say"] and writes(tools) == []
    assert held.gate.undo_state()["kind"] == "send"
    FakeTimer.made[-1].run_out()
    assert [c[0] for c in writes(tools)] == ["send_email"] and held.gate.undo_state() is None


@pytest.mark.parametrize("phrase", ["undo", "never mind", "stop", "don't send it", "Chief, cancel please"])
def test_it_can_be_stopped_in_words(held, tools, phrase):
    reply(held)
    handle_utterance(held, "do it")
    out = handle_utterance(held, phrase)
    assert out["say"].startswith("Stopped") and FakeTimer.made[-1].cancelled
    FakeTimer.made[-1].run_out()  # even if the timer fires anyway
    assert writes(tools) == []


def test_a_stopped_email_waits_for_you_with_its_full_text(held, tools):
    reply(held)
    handle_utterance(held, "do it")
    handle_utterance(held, "stop")
    card = next(iter(held.gate.pending.values()))
    assert card.actions[0].tool == "send_email" and "Friday works" in card.actions[0].args["body"]
    handle_utterance(held, "do it")
    FakeTimer.made[-1].run_out()
    assert len(writes(tools)) == 1


def test_a_failed_send_comes_back_instead_of_vanishing(held, tools, monkeypatch):
    reply(held)
    handle_utterance(held, "do it")
    monkeypatch.setattr(tools, "send_email", lambda **k: 1 / 0)
    FakeTimer.made[-1].run_out()
    assert len(held.gate.pending) == 1


def test_delay_zero_sends_at_once(rt, tools):
    reply(rt)
    handle_utterance(rt, "do it")
    assert [c[0] for c in writes(tools)] == ["send_email"]


# ---- undoing calendar changes --------------------------------------------------------------------


def test_undo_restores_titles_and_removes_new_events(rt, tools):
    handle_utterance(rt, "how's my week")
    before = {k: (e.title, e.description) for k, e in tools.events.items()}
    out = handle_utterance(rt, "do it")
    assert "Say undo" in out["say"]
    assert "Undone. 4 change(s) reversed." == handle_utterance(rt, "undo that")["say"]
    assert {k: (e.title, e.description) for k, e in tools.events.items()} == before


def test_undo_works_once_and_says_so_after(rt):
    handle_utterance(rt, "how's my week")
    handle_utterance(rt, "do it")
    handle_utterance(rt, "undo")
    assert "nothing to undo" in handle_utterance(rt, "undo")["say"]


def test_an_old_undo_button_cannot_undo_something_newer(rt):
    handle_utterance(rt, "how's my week")
    handle_utterance(rt, "do it")
    old = rt.gate.undo_state()["mark"]
    rt.gate.submit(Proposal("audit", "Add one", [ToolCall("create_event", {"title": "New", "start": "2026-10-23T09:00:00", "end": "2026-10-23T10:00:00"})], key="k2"))
    rt.gate.approve(next(iter(rt.gate.pending)))
    assert rt.gate.undo(old) is None and rt.gate.undo_state() is not None


def test_a_pending_card_wins_over_undoing_finished_work(rt, tools):
    handle_utterance(rt, "how's my week")
    handle_utterance(rt, "do it")
    reply(rt)
    out = handle_utterance(rt, "undo")
    assert out["say"].startswith("Okay, skipped") and rt.gate.last_undo  # the calendar change stays reversible


def test_an_unknown_change_is_not_offered_for_undo(rt):
    assert rt.gate.undo_state() is None and rt.gate.undo() is None
    assert rt.gate._before(ToolCall("patch_event", {"event_id": "nope", "fields": {"summary": "x"}})) is None
    assert rt.gate._reverse_of(ToolCall("send_email", {}), {}, None) is None


def test_the_default_delay_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("CHIEF_SEND_DELAY", raising=False)
    assert Config.from_env().send_delay == 30
    monkeypatch.setenv("CHIEF_SEND_DELAY", "0")
    assert Config.from_env().send_delay == 0
    monkeypatch.setenv("CHIEF_SEND_DELAY", "soon")
    assert Config.from_env().send_delay == 30

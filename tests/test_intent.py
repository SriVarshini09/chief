from __future__ import annotations

from chief.intent import handle_utterance


def test_trigger_phrase_runs_the_skill(rt):
    out = handle_utterance(rt, "Chief, how's my week looking?")
    assert out["proposal"] is not None and out["proposal"].skill == "audit"
    assert "Thursday" in out["say"]
    assert out["steps"][0] == "Running audit"


def test_approve_words_apply_the_pending_proposal(rt, tools):
    handle_utterance(rt, "why am I so tired")
    out = handle_utterance(rt, "Do it.")
    assert "change(s) made" in out["say"]
    assert any(t.startswith("Walk:") for t in (e.title for e in tools.events.values()))


def test_skip_words_run_nothing(rt, tools):
    handle_utterance(rt, "audit")
    out = handle_utterance(rt, "not now")
    assert out["say"] == "Okay, skipped."
    assert tools.calls == []


def test_approve_with_nothing_pending_is_not_a_command(rt, tools):
    out = handle_utterance(rt, "do it")
    assert tools.calls == []
    assert "I can help with" in out["say"]


def test_unknown_request_lists_skills(rt):
    out = handle_utterance(rt, "what's the capital of France")
    assert "audit" in out["say"] and out["proposal"] is None


def test_empty_input(rt):
    assert handle_utterance(rt, "   ")["say"] == "I didn't catch that."

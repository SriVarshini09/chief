"""Real speech arrives padded with filler words. Approving and skipping must still work, and must stay
plain string matching: nothing a model says can approve anything."""

from __future__ import annotations

import pytest

from chief.intent import _command, handle_utterance


@pytest.mark.parametrize("said", [
    "Do it", "Chief, do it.", "yeah do it please", "okay approve", "Chief do it now", "sounds good", "go ahead",
    "Hey Chief, go ahead please", "ok do it",
])
def test_natural_ways_of_approving(rt, tools, said):
    handle_utterance(rt, "how's my week looking?")
    out = handle_utterance(rt, said)
    assert "change(s) made" in out["say"], said
    assert any(c[0] == "create_event" for c in tools.calls)


@pytest.mark.parametrize("said", [
    "not now", "Chief skip that", "actually never mind", "no thanks", "skip it", "Chief, cancel", "nah",
])
def test_natural_ways_of_skipping(rt, tools, said):
    handle_utterance(rt, "how's my week looking?")
    out = handle_utterance(rt, said)
    assert out["say"] == "Okay, skipped.", said
    assert tools.calls == []
    assert rt.gate.pending == {}


def test_approve_words_do_nothing_when_nothing_is_pending(rt, tools):
    out = handle_utterance(rt, "Chief, do it please")
    assert tools.calls == []
    assert "I can help with" in out["say"]


def test_stripping_never_empties_a_command():
    assert _command("please") == "please"
    assert _command("chief") == "chief"
    assert _command("yes") == "yes"


def test_a_request_with_filler_still_reaches_its_skill(rt):
    out = handle_utterance(rt, "Chief, could you tell me how's my week looking please")
    assert out["proposal"] is not None and out["proposal"].skill == "audit"

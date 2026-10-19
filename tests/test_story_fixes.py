"""Regressions found by playing the demo story end to end."""

from __future__ import annotations

from chief.intent import handle_utterance


def test_read_only_results_are_never_queued_for_approval(rt, cards):
    out = handle_utterance(rt, "What's in my inbox?")
    assert out["proposal"] is not None and not out["proposal"].actions
    assert rt.gate.pending == {}  # a briefing is shown, not held
    assert len(cards) == 1  # ...but it is still delivered


def test_do_it_after_a_briefing_approves_the_real_proposal(rt, tools):
    handle_utterance(rt, "how's my week looking")  # a proposal with real actions
    handle_utterance(rt, "what's in my inbox")  # a read-only result afterwards
    out = handle_utterance(rt, "do it")
    assert "4 change(s) made" in out["say"]
    assert any(c[0] == "create_event" for c in tools.calls)


def test_reply_to_a_named_person_uses_the_users_words(rt, tools):
    out = handle_utterance(rt, "Reply to Sam and say Friday works")
    p = out["proposal"]
    assert p is not None and p.skill == "draft_reply"
    send = p.actions[0]
    assert send.tool == "send_email" and send.args["to"] == "sam@example.com"
    assert "Friday works." in send.args["body"]
    assert tools.drafts and "Friday works." in tools.drafts[0]["body"]  # saved as a draft
    assert not [c for c in tools.calls if c[0] == "send_email"]  # nothing sent yet


def test_nothing_is_sent_until_approved(rt, tools):
    handle_utterance(rt, "Reply to Sam and say Friday works")
    handle_utterance(rt, "do it")
    assert [c[0] for c in tools.calls if c[0] == "send_email"] == ["send_email"]


def test_reply_to_someone_with_no_waiting_mail_does_nothing(rt, tools):
    out = handle_utterance(rt, "Reply to Zebediah and say hello")
    assert out["proposal"] is None
    assert tools.drafts == []

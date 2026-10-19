"""The whole demo story, in mock mode, exactly as it is said on stage."""

from __future__ import annotations

from chief.intent import handle_utterance
from chief.tools.base import SAFE_WRITE_TOOLS, WRITE_TOOLS
from chief.tools.replay import STORY

PHRASES = [p for p, _ in STORY]


def play(rt):
    return [handle_utterance(rt, p) for p in PHRASES]


def test_the_story_says_the_right_things_in_order(rt):
    inbox, reply, week, do_it, prep, trip, add_it = play(rt)
    assert inbox["say"].startswith("Inbox:") and inbox["proposal"] and not inbox["proposal"].actions
    assert "draft reply to Sam" in reply["say"] and "Nothing is sent until you approve" in reply["say"]
    assert reply["proposal"].actions[0].tool == "send_email"
    assert "slept under 6 hours" in week["say"] and week["steps"][-1] == "Proposal ready: approve or skip"
    assert do_it["say"].startswith("Done. 4 change(s) made.")
    assert prep["say"].startswith("Prep for Acme pricing review") and "Dana" in prep["say"] and not prep["proposal"].actions
    assert trip["say"].startswith("Caltrain to Palo Alto") and trip["proposal"].actions[0].tool == "create_event"
    assert add_it["say"].startswith("Done. 1 change(s) made.")


def test_nothing_is_written_before_approval(rt, tools):
    handle_utterance(rt, PHRASES[0])
    handle_utterance(rt, PHRASES[1])
    handle_utterance(rt, PHRASES[2])
    done = {c[0] for c in tools.calls}
    assert not (done & WRITE_TOOLS), "a calendar change or a send happened before any approval"
    assert done <= SAFE_WRITE_TOOLS  # only the draft
    handle_utterance(rt, "do it")
    assert [c[0] for c in tools.calls].count("patch_event") == 3


def test_every_write_in_the_stream_follows_an_approval_for_the_same_utterance(rt):
    play(rt)
    approved = False
    for e in rt.bus.since(0):
        if e.kind == "heard":
            approved = False
        elif e.kind == "approved":
            approved = True
        elif e.tool in WRITE_TOOLS and e.kind in ("working", "write"):
            assert approved, f"{e.tool} ran without an approval in the same utterance (seq {e.seq})"


def test_the_reply_to_sam_is_never_sent(rt, tools):
    play(rt)  # "Do it" approves the most recent proposal (the audit), not the unsent email
    assert "send_email" not in [c[0] for c in tools.calls]
    assert any(p.skill == "draft_reply" for p in rt.gate.pending.values())


def test_the_activity_stream_narrates_every_step(rt):
    play(rt)
    events = rt.bus.since(0)
    assert [e.title for e in events if e.kind == "heard"] == PHRASES
    assert len([e for e in events if e.kind == "say"]) == len(PHRASES)
    working = {e.detail["ref"]: e for e in events if e.kind == "working"}
    finished = {e.detail["ref"] for e in events if e.kind in ("read", "write", "error")}
    assert working and set(working) == finished  # every started tool call has a result
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # each phrase is bracketed: heard ... say, with the skill step (or the approval) in between
    for phrase in PHRASES:
        i = next(k for k, e in enumerate(events) if e.kind == "heard" and e.title == phrase)
        j = next(k for k, e in enumerate(events) if k > i and e.kind == "say")
        kinds = [e.kind for e in events[i + 1 : j]]
        assert kinds, f"nothing narrated between hearing {phrase!r} and answering it"
        assert "step" in kinds or "approved" in kinds
    skills = [e.detail["skill"] for e in events if e.kind == "step" and e.title.startswith("Running ")]
    assert skills == ["email_brief", "draft_reply", "audit", "meeting_prep", "trip_planner"]
    assert [e.kind for e in events].count("approved") == 2


def test_mock_events_are_not_tagged_cached_or_simulated(rt):
    play(rt)
    for e in rt.bus.since(0):
        assert "cached" not in e.detail and "simulated" not in e.detail

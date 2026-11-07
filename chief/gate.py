"""The approval gate: the only place a write tool can run.

Skills propose. The gate holds proposals until a human approves (a Slack tap, a button, a CLI
prompt). It also re-checks that a skill only proposes tools it declared, so a confused or
prompt-injected skill cannot smuggle in, say, send_email.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from chief.activity import ActivityBus
from chief.memory import Memory
from chief.models import Proposal, ToolCall, when
from chief.skills.base import Skill
from chief.tools import Tools

_ids = itertools.count(1)


@dataclass
class Held:
    """An approved email in its last few seconds before it goes."""

    id: int
    proposal: Proposal
    action: ToolCall
    due: float
    timer: Any = None


SKIPPED = "No problem, I've skipped that one."


def changes(count: int) -> str:
    """'1 change', '4 changes': said the way a person would say it."""
    return "1 change" if count == 1 else f"{count} changes"


def made(count: int) -> str:
    """How Chief tells you an approval went through, with a little thanks for checking it first."""
    if count == 0:
        return "All done. Nothing needed changing."
    return f"All done. I made {changes(count)}. Thanks for looking it over first."


def results_text(results: list[dict]) -> str:
    """What to tell the person after an approval."""
    errors = [r for r in results if "error" in r]
    done = [r for r in results if "error" not in r and not (isinstance(r.get("result"), dict) and r["result"].get("held"))]
    if errors:
        rest = f" {changes(len(done))} went through, and the rest is waiting for you." if done else " It's waiting for you to retry or skip."
        return f"I'm sorry, that didn't go through: {errors[0]['error']}.{rest}"
    if len(done) < len(results):
        seconds = next(r["result"]["seconds"] for r in results if r not in done)
        return f"Sending in {seconds:g} seconds. You can still stop it."
    return made(len(results))


# Answers about places say where you are ("Sakura, 120 m away"), so they are never written to the state folder.
WHERE_SKILLS = {"nearby", "directions", "booking", "guide"}


def _saved(proposal) -> str:
    """What the diary on disk keeps about a card: never your mail or where you are, just enough to recognise it."""
    if proposal.skill in WHERE_SKILLS:
        return "(a places answer: kept in memory only)"
    if not proposal.actions:  # inbox briefs, prep, AI answers: they quote your mail
        return f"(an answer, {len(proposal.summary)} characters: not saved)"
    first = proposal.summary.splitlines()[0] if proposal.summary else ""
    return first if len(first) <= 80 else first[:77] + "..."


class ApprovalGate:
    def __init__(
        self,
        tools: Tools,
        memory: Memory,
        skills: dict[str, Skill],
        notify: Callable[[Proposal], None],
        bus: ActivityBus | None = None,
        send_delay: float = 0,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.tools, self.memory, self.skills, self.notify = tools, memory, skills, notify
        self.bus = bus or ActivityBus()
        self.pending: dict[str, Proposal] = {}
        self.names: dict[str, dict[str, str]] = {}  # proposal id -> event id -> what the card calls it
        self.since: dict[str, datetime] = {}  # proposal id -> when it was first shown
        self.send_delay, self.clock = send_delay, clock
        self.held: dict[int, Held] = {}  # approved emails that have not gone yet
        self.last_undo: list[ToolCall] = []  # what reverses the last approval's calendar changes
        self.undo_mark = 0
        self.timer = threading.Timer
        self._lock = threading.RLock()  # parallel tasks submit while the user approves

    def _emit_proposal(self, p: Proposal) -> None:
        self.bus.emit(
            "proposal",
            p.summary,
            detail={"id": p.id, "skill": p.skill, "actions": [a.describe() for a in p.actions], "sources": p.sources, "read_only": not p.actions},
        )

    def is_pending(self, key: str) -> bool:
        with self._lock:
            return any(p.key == key for p in self.pending.values())

    def reorder(self, ids: list[str]) -> None:
        """Make these pending proposals the newest, in this order (parallel tasks finish in any order, but
        "do it" means the most recent one in the order the user heard them)."""
        with self._lock:
            for i in ids:
                if i in self.pending:
                    self.pending[i] = self.pending.pop(i)

    def supersede(self, old_id: str, new: Proposal) -> None:
        """Replace a pending proposal with an edited one. The edit is a NEW proposal: it goes through
        submit() (declared-tools check, card, activity event) like any other, and nothing in it has run.
        The old one is dropped without a decision record, so the thread/situation is not marked rejected."""
        if old_id not in self.pending:
            raise KeyError(old_id)
        self.submit(new)  # raises before any state changes if the edit is not allowed
        with self._lock:
            old = self.pending.pop(old_id, None)
        if old is not None:
            self.bus.emit(
                "skipped", f"Replaced by an edited version: {old.summary}",
                detail={"id": old.id, "skill": old.skill, "superseded_by": new.id},
            )
            self.memory.log("superseded", id=old.id, by=new.id)

    def submit(self, proposal: Proposal) -> None:
        skill = self.skills.get(proposal.skill)
        if skill is None:
            raise PermissionError(f"unknown skill: {proposal.skill}")
        for action in proposal.actions:
            if action.tool not in skill.propose_tools:
                raise PermissionError(f"skill '{skill.name}' may not propose {action.tool}")
        if not proposal.actions:
            # A read-only result (briefing, prep): show it, but never queue it. Otherwise a later
            # "do it" could land on a card with nothing to approve instead of the real proposal.
            self.memory.log("answered", id=proposal.id, skill=proposal.skill, summary=_saved(proposal))
            self._emit_proposal(proposal)
            self.notify(proposal)
            return
        self.names[proposal.id] = self._event_names(proposal)
        self.since.setdefault(proposal.id, self.clock())
        with self._lock:
            self.pending[proposal.id] = proposal
        self.memory.log("proposed", id=proposal.id, skill=proposal.skill, summary=_saved(proposal))
        self._emit_proposal(proposal)
        self.notify(proposal)

    def approve(self, proposal_id: str) -> list[dict]:
        with self._lock:
            proposal = self.pending.pop(proposal_id)
            self.names.pop(proposal_id, None)
        self.bus.emit("approved", f"Approved: {proposal.summary}", detail={"id": proposal.id, "skill": proposal.skill})
        results: list[dict] = []
        reverse: list[ToolCall] = []
        for i, action in enumerate(proposal.actions):
            if action.tool == "send_email" and self.send_delay > 0:
                self._hold(proposal, action)
                results.append({"tool": action.tool, "result": {"held": True, "seconds": self.send_delay}})
                continue
            try:
                before = self._before(action)
                result = self.tools.call(action.tool, action.args)
            except Exception as exc:  # what already ran stays done, and only the rest waits for a retry
                results.append({"tool": action.tool, "error": str(exc)})
                self.memory.log("action_failed", id=proposal.id, tool=action.tool, error=str(exc))
                self._remember_undo(reverse)
                self._wait_again(proposal, proposal.actions[i:])
                return results
            results.append({"tool": action.tool, "result": result})
            step = self._reverse_of(action, result, before)
            if step:
                reverse.append(step)
        self._remember_undo(reverse)
        self.memory.record(key=proposal.key, skill=proposal.skill, decision="accepted", summary=_saved(proposal))
        self.memory.log("approved", id=proposal.id, results=results)
        return results

    def _wait_again(self, proposal: Proposal, left: list[ToolCall]) -> None:
        """Running a finished step twice would duplicate an event, so a retry covers only what is left."""
        done = len(proposal.actions) - len(left)
        if done:
            proposal.summary = f"{done} of {len(proposal.actions)} steps went through. Retry the rest?"
            proposal.actions = left
        with self._lock:
            self.pending[proposal.id] = proposal
        self._emit_proposal(proposal)
        self.notify(proposal)

    # ---- undo: calendar changes can be reversed, and an email can be stopped until it goes ---------

    def _event_names(self, proposal: Proposal) -> dict[str, str]:
        """'"Standup", Thu Oct 22, 10:00 AM' for each event a card would change, so the card can say which."""
        ids = {str(a.args.get("event_id")) for a in proposal.actions if a.tool in ("patch_event", "delete_event")}
        if not ids:
            return {}
        try:
            now = self.clock()
            events = self.tools.list_events(now - timedelta(days=90), now + timedelta(days=365))
        except Exception:
            return {}  # the card falls back to the event id: still true, just less friendly
        return {e.id: f'"{e.title}", {when(e.start)}' for e in events if e.id in ids}

    def plain(self, proposal: Proposal) -> list[str]:
        """What each action does, in words, naming the events it touches."""
        names = self.names.get(proposal.id, {})
        return [a.plain(names) for a in proposal.actions]

    def _before(self, action: ToolCall) -> dict | None:
        """The fields a calendar change will overwrite. None means it cannot be reversed."""
        if action.tool != "patch_event":
            return None
        now = self.clock()
        event = next((e for e in self.tools.list_events(now - timedelta(days=90), now + timedelta(days=365)) if e.id == action.args["event_id"]), None)
        if event is None:
            return None
        known = {"summary": event.title, "description": event.description, "start": event.start.isoformat(), "end": event.end.isoformat()}
        fields = action.args.get("fields") or {}
        return {k: known[k] for k in fields} if set(fields) <= set(known) else None

    @staticmethod
    def _reverse_of(action: ToolCall, result: Any, before: dict | None) -> ToolCall | None:
        if action.tool == "create_event" and isinstance(result, dict) and result.get("id"):
            return ToolCall("delete_event", {"event_id": result["id"]})
        if action.tool == "patch_event" and before:
            return ToolCall("patch_event", {"event_id": action.args["event_id"], "fields": before})
        return None

    def _remember_undo(self, reverse: list[ToolCall]) -> None:
        with self._lock:
            self.last_undo = reverse
            self.undo_mark += 1

    def _hold(self, proposal: Proposal, action: ToolCall) -> None:
        held = Held(next(_ids), proposal, action, time.monotonic() + self.send_delay)
        held.timer = self.timer(self.send_delay, self._release, [held.id])
        held.timer.daemon = True
        with self._lock:
            self.held[held.id] = held
        held.timer.start()
        self.bus.emit("step", f"Sending in {self.send_delay:g} seconds", detail={"id": proposal.id, "skill": proposal.skill})

    def _release(self, held_id: int) -> None:
        with self._lock:
            held = self.held.pop(held_id, None)
        if held is None:  # stopped in time
            return
        try:
            self.tools.call(held.action.tool, held.action.args)
            self.memory.log("sent", id=held.proposal.id)
        except Exception as exc:
            self.memory.log("action_failed", id=held.proposal.id, tool=held.action.tool, error=str(exc))
            self.memory.forget(held.proposal.key)
            self._wait_again(held.proposal, [held.action])

    def _stop_send(self) -> None:
        with self._lock:
            held = self.held.pop(max(self.held))
        held.timer.cancel()
        self.memory.forget(held.proposal.key)
        self.memory.log("send_stopped", id=held.proposal.id)
        again = Proposal(held.proposal.skill, "Stopped. This email was not sent. Send it after all?", [held.action], held.proposal.key, list(held.proposal.sources))
        self.submit(again)

    def undo_state(self) -> dict | None:
        """What can be undone right now, for a button. `mark` ties a button to this exact thing."""
        with self._lock:
            if self.held:
                held = self.held[max(self.held)]
                return {"kind": "send", "mark": f"s{held.id}", "label": "Stop sending", "seconds": max(0, round(held.due - time.monotonic()))}
            if self.last_undo:
                return {"kind": "changes", "mark": f"c{self.undo_mark}", "label": "Undo"}
        return None

    def undo(self, mark: str | None = None) -> str | None:
        """Reverse the latest thing. Returns what to say, or None if there is nothing (or `mark` is stale)."""
        state = self.undo_state()
        if state is None or (mark is not None and mark != state["mark"]):
            return None
        if state["kind"] == "send":
            self._stop_send()
            return "Stopped. Nothing was sent, and the email is waiting for you."
        with self._lock:
            steps, self.last_undo = self.last_undo, []
        failed = 0
        for step in reversed(steps):
            try:
                self.tools.call(step.tool, step.args)
            except Exception as exc:
                failed += 1
                self.memory.log("undo_failed", tool=step.tool, error=str(exc))
        self.memory.log("undone", steps=len(steps), failed=failed)
        if failed:
            return f"I'm sorry, I could only reverse {len(steps) - failed} of {len(steps)} changes. Please check your calendar."
        return f"Undone. I've reversed {changes(len(steps))}, so it's back the way it was."

    def skip(self, proposal_id: str) -> None:
        with self._lock:
            proposal = self.pending.pop(proposal_id)
            self.names.pop(proposal_id, None)
        self.bus.emit("skipped", f"Skipped: {proposal.summary}", detail={"id": proposal.id, "skill": proposal.skill})
        self.memory.record(key=proposal.key, skill=proposal.skill, decision="rejected", summary=_saved(proposal))
        self.memory.log("skipped", id=proposal.id)

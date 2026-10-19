"""The approval gate: the only place a write tool can run.

Skills propose. The gate holds proposals until a human approves (a Slack tap, a button, a CLI
prompt). It also re-checks that a skill only proposes tools it declared, so a confused or
prompt-injected skill cannot smuggle in, say, send_email.
"""

from __future__ import annotations

import threading
from typing import Callable

from chief.activity import ActivityBus
from chief.memory import Memory
from chief.models import Proposal
from chief.skills.base import Skill
from chief.tools import Tools


class ApprovalGate:
    def __init__(
        self,
        tools: Tools,
        memory: Memory,
        skills: dict[str, Skill],
        notify: Callable[[Proposal], None],
        bus: ActivityBus | None = None,
    ):
        self.tools, self.memory, self.skills, self.notify = tools, memory, skills, notify
        self.bus = bus or ActivityBus()
        self.pending: dict[str, Proposal] = {}
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
            self.memory.log("answered", id=proposal.id, skill=proposal.skill, summary=proposal.summary)
            self._emit_proposal(proposal)
            self.notify(proposal)
            return
        with self._lock:
            self.pending[proposal.id] = proposal
        self.memory.log("proposed", id=proposal.id, skill=proposal.skill, summary=proposal.summary)
        self._emit_proposal(proposal)
        self.notify(proposal)

    def approve(self, proposal_id: str) -> list[dict]:
        with self._lock:
            proposal = self.pending.pop(proposal_id)
        self.bus.emit("approved", f"Approved: {proposal.summary}", detail={"id": proposal.id, "skill": proposal.skill})
        results: list[dict] = []
        for action in proposal.actions:
            try:
                results.append({"tool": action.tool, "result": self.tools.call(action.tool, action.args)})
            except Exception as exc:  # leave it undecided so a later check-in can re-propose
                results.append({"tool": action.tool, "error": str(exc)})
                self.memory.log("action_failed", id=proposal.id, tool=action.tool, error=str(exc))
                return results
        self.memory.record(key=proposal.key, skill=proposal.skill, decision="accepted", summary=proposal.summary)
        self.memory.log("approved", id=proposal.id, results=results)
        return results

    def skip(self, proposal_id: str) -> None:
        with self._lock:
            proposal = self.pending.pop(proposal_id)
        self.bus.emit("skipped", f"Skipped: {proposal.summary}", detail={"id": proposal.id, "skill": proposal.skill})
        self.memory.record(key=proposal.key, skill=proposal.skill, decision="rejected", summary=proposal.summary)
        self.memory.log("skipped", id=proposal.id)

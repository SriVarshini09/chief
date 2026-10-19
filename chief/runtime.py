"""Runtime: wires tools, skills, memory and the approval gate together."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from chief.activity import ActivityBus
from chief.config import Config
from chief.conversation import Conversation
from chief.gate import ApprovalGate
from chief.llm import LLM
from chief.memory import Memory
from chief.models import Proposal
from chief.skills import Context, load_skills
from chief.tools import ScopedTools, Tools, get_tools
from chief.tools.recording import RecordingTools


def format_card(p: Proposal) -> str:
    lines = [f"[{p.skill}] {p.summary}", ""]
    lines += [f"  - {a.describe()}" for a in p.actions]
    lines += [f"  source: {s}" for s in p.sources]
    lines.append(f"  (proposal {p.id}: approve or skip)" if p.actions else f"  (proposal {p.id}: read-only, nothing to approve)")
    return "\n".join(lines)


class Runtime:
    def __init__(
        self,
        cfg: Config,
        tools: Tools | None = None,
        notify: Callable[[Proposal], None] | None = None,
        llm: LLM | None = None,
        bus: ActivityBus | None = None,
    ):
        self.cfg = cfg
        self.bus = bus or ActivityBus()
        self.inner_tools = tools or get_tools(cfg)  # the real or mock tools
        self.tools = RecordingTools(self.inner_tools, self.bus)  # what skills and the gate use: every call is reported
        self.llm = llm or LLM(cfg)
        self.memory = Memory(cfg.state_dir)
        self.conversation = Conversation()  # short-term context for follow-ups (chief/conversation.py)
        self.skills = load_skills()
        self.gate = ApprovalGate(
            self.tools, self.memory, self.skills, notify or (lambda p: print(format_card(p))), bus=self.bus
        )

    def now(self) -> datetime:
        return self.cfg.now or datetime.now()

    def run_skill(self, name: str, now: datetime | None = None, request: str = "") -> Proposal | None:
        """Run one skill. None means it chose to stay silent or this situation was already handled."""
        now = now or self.now()
        skill = self.skills[name]
        self.bus.emit("step", f"Running {name}", detail={"skill": name, "request": request})
        ctx = Context(ScopedTools(self.tools, skill.allowed_tools, skill.name), self.llm, self.memory, now, request, self.conversation.history())
        proposal = skill.run(ctx)
        if proposal is None:
            self.memory.log("no_reply", skill=name)
            self.bus.emit("step", f"{name}: nothing to report", detail={"skill": name})
            return None
        if self.memory.decided(proposal.key) or self.gate.is_pending(proposal.key):
            self.memory.log("duplicate", skill=name, key=proposal.key)
            return None
        self.gate.submit(proposal)
        return proposal

    def tick(self, now: datetime | None = None) -> list[Proposal]:
        """One heartbeat: run every heartbeat skill. Silent unless something is worth saying."""
        now = now or self.now()
        if not self.cfg.in_active_hours(now):
            self.memory.log("outside_active_hours", hour=now.hour)
            return []
        out = []
        for skill in self.skills.values():
            if skill.heartbeat:
                p = self.run_skill(skill.name, now)
                if p:
                    out.append(p)
        return out

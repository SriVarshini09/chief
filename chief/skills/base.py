"""The skill contract. A skill is one file that defines SKILL = Skill(...).

    SKILL = Skill(
        name="audit",
        description="...",
        triggers=["how's my week"],
        allowed_tools={"list_events", "read_health"},   # read tools it may call while running
        propose_tools={"patch_event", "create_event"},  # writes it may ask the gate to run
        run=run,
    )

`run(ctx)` returns a Proposal, or None to stay silent (the NO_REPLY default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from chief.llm import LLM
from chief.memory import Memory
from chief.models import Proposal
from chief.tools import ScopedTools


@dataclass
class Context:
    tools: ScopedTools  # reads only; writes must be proposed
    llm: LLM
    memory: Memory
    now: datetime
    request: str = ""  # what the user said, when the skill was asked for (empty on a heartbeat)
    history: list[dict] = field(default_factory=list)  # recent turns [{"user", "chief"}], oldest first; context only


@dataclass
class Skill:
    name: str
    description: str
    run: Callable[[Context], Proposal | None]
    triggers: list[str] = field(default_factory=list)
    allowed_tools: set[str] = field(default_factory=set)
    propose_tools: set[str] = field(default_factory=set)
    heartbeat: bool = True  # also run on the scheduled check-in

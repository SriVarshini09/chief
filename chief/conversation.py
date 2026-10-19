"""Short-term conversation context, and the follow-ups that use it.

A follow-up is a short phrase that only makes sense after the previous answer: "make that shorter",
"and what about Friday?", "make that one a walk". Recognition is plain pattern matching on what the
user said, never a model's decision, and nothing here can write:

  * an edit to a pending proposal builds a NEW Proposal and hands it to gate.supersede(), which runs
    gate.submit() again (declared-tools check, card, activity event). It is pending until approved.
  * "what about Friday" re-runs a skill through the normal task path, so its proposal is gated as usual.
  * there is no way to edit something already approved: those phrases only see PENDING proposals.

The Conversation keeps the last few turns (for the general skill's context), the last skill, the last
result and the last proposal id, and hands out the short task ids ("t1", "t2") used by the Stage lanes.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from chief.activity import task_scope
from chief.models import Proposal, ToolCall
from chief.skills.draft_reply import revise_body

if TYPE_CHECKING:
    from chief.runtime import Runtime

MAX_TURNS = 6
EDITABLE_BODY_TOOLS = {"send_email", "create_draft"}  # actions whose args["body"] can be reworded
DAY_SKILLS = {"general", "trip_planner"}  # skills whose request text can name another day


@dataclass
class Task:
    """One piece of an utterance, run by one skill. `id` is stamped on every event it emits."""

    id: str
    skill: str
    request: str
    say: str = ""
    proposal_id: str | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "skill": self.skill, "say": self.say, "proposal_id": self.proposal_id}


@dataclass
class Turn:
    user: str
    say: str
    skills: list[str] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)


@dataclass
class FollowUp:
    say: str
    proposal: Proposal | None = None  # a NEW proposal, already submitted to the gate (pending, unapproved)
    rerun: tuple[str, str] | None = None  # (skill, request): the caller runs it as an ordinary task


class Conversation:
    def __init__(self, max_turns: int = MAX_TURNS):
        self.turns: deque[Turn] = deque(maxlen=max_turns)
        self.last_tasks: list[Task] = []
        self.last_result: Proposal | None = None
        self.last_proposal_ids: list[str] = []  # proposals of the latest answer, in the order they were spoken
        self._origin: dict[str, str] = {}  # proposal id -> task id that made it (so an approval lands in its lane)
        self._count = 0
        self._lock = threading.Lock()

    @property
    def last_skill(self) -> str | None:
        return self.last_tasks[-1].skill if self.last_tasks else None

    @property
    def last_proposal_id(self) -> str | None:
        return self.last_proposal_ids[-1] if self.last_proposal_ids else None

    def new_task_id(self) -> str:
        with self._lock:
            self._count += 1
            return f"t{self._count}"

    def task_of(self, proposal_id: str) -> str | None:
        return self._origin.get(proposal_id)

    def record(self, user: str, say: str, tasks: list[Task] | None = None, proposals: list[Proposal] | None = None) -> None:
        tasks, proposals = tasks or [], proposals or []
        self.turns.append(Turn(user, say, [t.skill for t in tasks], [p.id for p in proposals]))
        if tasks:
            self.last_tasks = list(tasks)
            self.last_proposal_ids = [p.id for p in proposals]
            self.last_result = proposals[-1] if proposals else None
            for t in tasks:
                if t.proposal_id:
                    self._origin[t.proposal_id] = t.id
            while len(self._origin) > 50:
                self._origin.pop(next(iter(self._origin)))

    def replace(self, old_id: str, new: Proposal) -> None:
        """An edited proposal takes the place of the one it replaced."""
        self.last_proposal_ids = [new.id if i == old_id else i for i in self.last_proposal_ids]
        if self.last_result is not None and self.last_result.id == old_id:
            self.last_result = new
        if old_id in self._origin:
            self._origin[new.id] = self._origin[old_id]
        for t in self.last_tasks:
            if t.proposal_id == old_id:
                t.proposal_id = new.id

    def history(self) -> list[dict]:
        """The recent turns as plain dicts, oldest first. For the general skill's context only."""
        return [{"user": t.user, "chief": t.say} for t in self.turns]


# --- describing what is pending -------------------------------------------------------------------


def describe_action(a: ToolCall) -> str:
    args = a.args
    if a.tool == "create_event":
        return f"add '{args.get('title', 'an event')}' to your calendar"
    if a.tool == "patch_event":
        name = (args.get("fields") or {}).get("summary")
        return f"change a calendar event to '{name}'" if name else "change a calendar event"
    if a.tool == "send_email":
        return f"send an email to {args.get('to', 'someone')}"
    if a.tool == "create_draft":
        return f"save a draft to {args.get('to', 'someone')}"
    return a.tool


def brief(p: Proposal) -> str:
    acts = [describe_action(a) for a in p.actions[:3]]
    more = f" and {len(p.actions) - 3} more" if len(p.actions) > 3 else ""
    return f"{p.skill}: " + "; ".join(acts) + more


def recent_pending(rt: "Runtime") -> list[Proposal]:
    return list(rt.gate.pending.values())  # oldest first; the last one is the latest


def pending_note(rt: "Runtime") -> str:
    """One plain sentence on what is waiting for approval, and which words do what."""
    pend = recent_pending(rt)
    if not pend:
        return ""
    if len(pend) == 1:
        return f"Waiting for your OK: {brief(pend[0])}. Say 'do it' to approve or 'skip' to drop it."
    items = " ".join(f"({i}) {brief(p)}." for i, p in enumerate(pend, 1))
    from_last = [p for p in pend if p.id in set(rt.conversation.last_proposal_ids)]
    note = f"'do it' approves only the latest ({len(pend)})."
    if len(from_last) > 1:
        note += f" 'approve all' approves the {len(from_last)} from my last answer."
    return f"Waiting for your OK on {len(pend)} things: {items} {note} 'skip' drops the latest."


# --- follow-up phrases ----------------------------------------------------------------------------

_F = r"(?:(?:ok(?:ay)?|hey|chief|please|can you|could you|and|then)\s+)*"
_THAT = r"(?:that|it|this|the (?:reply|draft|email|message|note)|that (?:reply|draft|email|message|note))"
_DAY = (
    r"(?:(?:next|this)\s+)?(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)"
)
_WALK = re.compile(rf"^{_F}(?:make|turn|change)\s+(?P<ref>.+?)\s+(?:in)?(?:to\s+)?(?:a\s+)?(?:walk(?:ing)?)(?:\s+(?:call|meeting|and talk))?(?:\s+instead)?$")
_TONE = re.compile(
    rf"^{_F}(?:make|rewrite|reword|keep)\s+{_THAT}\s+(?:a\s+(?:bit|little)\s+|much\s+|way\s+|bit\s+)?(?P<what>.+)$"
)
_TONE2 = re.compile(rf"^{_F}(?P<what>shorten|tighten up|tighten|soften)\s+{_THAT}$")
_TONE3 = re.compile(rf"^(?P<what>shorter|more formal|less formal|more casual|friendlier|warmer)(?:\s+please)?$")
_DAY_RE = re.compile(rf"^{_F}(?:(?:what|how) about|same (?:for|on)|(?:for|on))\s+(?P<day>{_DAY})(?:\s+(?:instead|then|too|as well))?$")

_TONE_WORDS = ("short", "brief", "concise", "tight", "formal", "polite", "professional", "casual", "relax", "friendl", "warm", "nicer", "soft")


def resolve(rt: "Runtime", said: str) -> FollowUp | None:
    """A FollowUp if `said` (already normalised) is a follow-up phrase we handle, else None."""
    said = " ".join(said.split())
    if m := _WALK.match(said):
        return _make_walk(rt, m.group("ref"))
    m = _TONE.match(said) or _TONE2.match(said) or _TONE3.match(said)
    if m:
        what = m.group("what").strip()
        known = any(w in what for w in _TONE_WORDS)
        if known or (rt.llm.enabled and _bodies(rt)):
            return _revise(rt, what)
        return None  # "make it work", with no draft or no model: not ours
    if m := _DAY_RE.match(said):
        return _other_day(rt, m.group("day"))
    return None


# --- "make that shorter / more formal" ------------------------------------------------------------


def _bodies(rt: "Runtime") -> list[Proposal]:
    return [p for p in recent_pending(rt) if any(a.tool in EDITABLE_BODY_TOOLS and a.args.get("body") for a in p.actions)]


def _revise(rt: "Runtime", instruction: str) -> FollowUp:
    drafts = _bodies(rt)
    if not drafts:
        return FollowUp("There's no draft waiting for your approval, so there's nothing to reword. Ask me to draft a reply first.")
    old = drafts[-1]
    idx = next(i for i, a in enumerate(old.actions) if a.tool in EDITABLE_BODY_TOOLS and a.args.get("body"))
    action = old.actions[idx]
    new_body = revise_body(rt.llm, action.args["body"], instruction)
    if new_body is None:
        if any(w in instruction for w in _TONE_WORDS[:4]):
            return FollowUp("It's already about as short as I can make it without losing the point.")
        if rt.llm.enabled or any(w in instruction for w in _TONE_WORDS):
            return FollowUp("It already reads that way, so I left it alone.")
        return FollowUp("Without a language model I can only shorten a draft or change how formal or friendly it is.")
    actions = list(old.actions)
    actions[idx] = ToolCall(action.tool, {**action.args, "body": new_body})
    base = old.summary.split("\nRevised (")[0]
    new = Proposal(
        skill=old.skill,
        summary=f"{base}\nRevised ({instruction}):\n{new_body}",
        actions=actions,
        key=old.key,
        sources=list(old.sources),
    )
    with task_scope(rt.conversation.task_of(old.id)):  # the new card appears in the lane of the one it replaces
        rt.gate.supersede(old.id, new)
    rt.conversation.replace(old.id, new)
    return FollowUp(f"Done ({instruction}). New text:\n{new_body}\nNothing is sent until you approve it.", proposal=new)


# --- "make that one a walk" -----------------------------------------------------------------------


def _title(summary: str) -> str:
    return re.sub(r"^(\[async\]\s*|walk:\s*)", "", summary, flags=re.I).strip()


def _norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split())


def _make_walk(rt: "Runtime", ref: str) -> FollowUp:
    editable = [p for p in recent_pending(rt) if any(a.tool == "patch_event" for a in p.actions)]
    if not editable:
        return FollowUp("There's no calendar change waiting for your approval, so there's nothing to turn into a walk.")
    old = editable[-1]
    hint = _norm_title(re.sub(r"\b(that|it|this|one|the|meeting|call|event)\b", " ", ref))
    cands = []
    for i, a in enumerate(old.actions):
        if a.tool == "patch_event":
            summary = str((a.args.get("fields") or {}).get("summary") or "")
            if summary:
                cands.append((i, a, _title(summary), summary.lower().startswith("walk:")))
    if hint:
        cands = [c for c in cands if hint in _norm_title(c[2])]
        if not cands:
            return FollowUp(f"I don't see an event matching '{hint}' in what's waiting for approval.")
    open_ones = [c for c in cands if not c[3]]
    if not open_ones:
        return FollowUp(f"{cands[0][2]} is already a walking call in the pending plan.") if cands else FollowUp("I can't tell which event you mean.")
    if len(open_ones) > 1:
        names = ", ".join(c[2] for c in open_ones)
        return FollowUp(f"Which one should be a walk: {names}? Say, for example, 'make the {open_ones[0][2].lower()} a walk'.")
    i, a, title, _ = open_ones[0]
    actions = list(old.actions)
    actions[i] = ToolCall(
        "patch_event",
        {"event_id": a.args["event_id"], "fields": {"summary": f"Walk: {title}", "description": "Walking call, no screen."}},
    )
    note = ""
    if str((a.args.get("fields") or {}).get("summary", "")).lower().startswith("[async]"):
        kept = [x for x in actions if not (x.tool == "create_event" and "focus block" in str(x.args.get("title", "")).lower())]
        if len(kept) != len(actions):  # the focus block was only valid with both meetings async
            actions, note = kept, " I dropped the focus block, since it needed both meetings to be async."
    new = Proposal(
        skill=old.skill,
        summary="Updated plan: " + "; ".join(describe_action(x) for x in actions) + ". Approve?",
        actions=actions,
        key=old.key,
        sources=list(old.sources),
    )
    with task_scope(rt.conversation.task_of(old.id)):  # the new card appears in the lane of the one it replaces
        rt.gate.supersede(old.id, new)
    rt.conversation.replace(old.id, new)
    return FollowUp(f"Okay, {title} becomes a walking call.{note} Nothing has changed yet: this is a new proposal.", proposal=new)


# --- "and what about Friday?" ----------------------------------------------------------------------


def _other_day(rt: "Runtime", day: str) -> FollowUp | None:
    conv = rt.conversation
    prior = next((t for t in reversed(conv.last_tasks) if t.skill in DAY_SKILLS), None)
    if prior is None:
        if not conv.last_tasks:
            return None  # nothing to refer back to: treat it as a fresh request
        skill = conv.last_tasks[-1].skill
        return FollowUp(f"I can't re-run {skill} for another day yet. Ask me directly, for example 'how's my week' or 'prep me for my next meeting'.")
    if prior.skill == "trip_planner":
        if day not in ("today", "tomorrow"):
            return FollowUp("I can plan a trip for today or tomorrow. Which one?")
        request = re.sub(r"\b(tomorrow|today)\b", "", prior.request, flags=re.I).strip() + (" tomorrow" if day == "tomorrow" else "")
    else:
        request = f"{prior.request.split(chr(10) + '(Follow-up')[0]}\n(Follow-up: the same request, but for {day} instead.)"
    return FollowUp(f"Checking {day}.", rerun=(prior.skill, request))

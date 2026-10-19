"""Turns what you said (voice transcript or typed) into one or more actions.

Order of checks, cheapest and safest first:
  1. approve / skip / undo words, applied to PENDING proposals ("do it" = the most recent one,
     "approve all" = everything from my last answer)
  2. follow-ups that refer to the last answer ("make that shorter", "and what about Friday?",
     "make that one a walk"): see chief/conversation.py
  3. a request that holds several tasks ("prep me for Dana and find me a Caltrain after 6") is split,
     and the tasks run at the same time, each with its own task id on every activity event
  4. a skill's trigger phrases
  5. (optional) the LLM picks one skill name from the fixed list
  6. (optional) with an LLM, the read-only "general" skill answers anything else
  7. otherwise: say what Chief can do

The approve path is plain string matching: a model never decides to approve something. A model may help
split a request or pick a skill, but whatever it picks still ends at the approval gate.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from chief.activity import task_scope
from chief.conversation import Task, pending_note, resolve
from chief.models import Proposal
from chief.runtime import Runtime

APPROVE = {
    "do it", "approve", "approved", "yes", "yeah", "yep", "go ahead", "add it", "send it", "confirm", "ok do it",
    "do that", "sounds good", "looks good", "go for it", "yes do it", "sure", "make it so", "that works",
}
APPROVE_ALL = {
    "approve all", "approve both", "approve everything", "do both", "do all", "do all of it", "do it all",
    "do them all", "yes to all", "yes to both", "go ahead with both", "go ahead with all",
}
SKIP = {
    "skip", "no", "nope", "cancel", "not now", "never mind", "dismiss", "skip that", "skip it", "no thanks",
    "don't do it", "dont do it", "nah", "leave it",
}
SKIP_ALL = {"skip all", "skip both", "cancel all", "cancel both", "dismiss all", "never mind both", "never mind all"}
UNDO = {"undo that", "undo it", "undo", "scratch that", "forget it", "forget that", "take that back"}

# Spoken commands arrive padded ("Chief, do it please", "actually never mind"). Strip these from both ends
# before matching the command sets above. Only plain words are removed; nothing a model said is involved.
_LEAD_FILLER = {"chief", "hey", "hi", "ok", "okay", "alright", "actually", "please", "so", "well", "just", "then", "and", "yeah", "yep", "sure"}
_TRAIL_FILLER = {"chief", "please", "now", "thanks", "thank", "you", "then", "ok", "okay"}


def _command(said: str) -> str:
    """`said` without leading/trailing filler words; the original when stripping would leave nothing."""
    words = said.split()
    while len(words) > 1 and words[0] in _LEAD_FILLER:
        words.pop(0)
    while len(words) > 1 and words[-1] in _TRAIL_FILLER:
        words.pop()
    return " ".join(words) or said

MAX_PARALLEL = 4  # tasks per utterance, and threads used for them
NOTHING = "Nothing worth your attention there."
NO_DESTINATION = "I couldn't plan a trip without a destination. Try 'head to Palo Alto after this and find me a Caltrain after 6'."

_SEP = re.compile(r"\s*,?\s*\b(?:and\s+also|and\s+then|as\s+well\s+as|and|also|then|plus)\b\s*|\s*[;,]\s*", re.I)
_HAS_SEP = re.compile(r"\b(?:and|also|then|plus)\b", re.I)
SPLIT_SYSTEM = (
    "Decide whether the user's request holds two or more separate things to do. If so reply with a JSON array of "
    "2 to 4 strings, each the user's own words for one task, copied exactly from the request, adding nothing. If it "
    "is a single task reply with []. JSON only."
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("’", "'")).strip()


def _match_skill(rt: Runtime, text: str) -> str | None:
    best, best_len = None, 0
    for skill in rt.skills.values():
        for trig in skill.triggers:
            t = _norm(trig)
            if t and t in text and len(t) > best_len:
                best, best_len = skill.name, len(t)
    return best


def _llm_pick(rt: Runtime, text: str) -> str | None:
    if not rt.llm.enabled:
        return None
    names = list(rt.skills)
    choice = rt.llm.complete(
        "Pick the single best skill name for the request, or reply exactly: none. Reply with the name only.\n"
        + "\n".join(f"- {s.name}: {s.description}" for s in rt.skills.values()),
        text,
        "none",
    )
    choice = choice.strip().strip("`'\".").lower()  # not _norm: skill names contain underscores
    return choice if choice in names else None


def _route(rt: Runtime, text: str, said: str) -> str | None:
    name = _match_skill(rt, said) or _llm_pick(rt, text)
    if not name and rt.llm.enabled and "general" in rt.skills:
        name = "general"  # nothing matched: let the model answer with read-only tools
    return name


# --- splitting one utterance into several tasks ----------------------------------------------------


def _split_by_triggers(rt: Runtime, text: str) -> list[tuple[str, str]] | None:
    """Split on 'and' / 'also' / 'then' / commas, but only where each side matches a DIFFERENT skill's
    trigger. A piece that matches nothing ("say Friday works") stays with the piece before it; if the same
    skill shows up twice the request is left whole (one skill handles it)."""
    pieces, pos = [], 0
    for m in _SEP.finditer(text):
        if m.start() > pos:
            pieces.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        pieces.append((pos, len(text)))
    groups: list[list] = []  # [skill, start, end]
    lead: int | None = None
    for s, e in pieces:
        skill = _match_skill(rt, _norm(text[s:e]))
        if skill is None:
            if groups:
                groups[-1][2] = e
            elif lead is None:
                lead = s
        elif groups and groups[-1][0] == skill:
            groups[-1][2] = e
        else:
            groups.append([skill, lead if (not groups and lead is not None) else s, e])
    if not 2 <= len(groups) <= MAX_PARALLEL or len({g[0] for g in groups}) != len(groups):
        return None
    return [(skill, text[s:e].strip(" ,;")) for skill, s, e in groups]


def _split_by_llm(rt: Runtime, text: str, said: str) -> list[tuple[str, str]] | None:
    """Optional: only with a model, only when no trigger phrase matched at all, and only if the model's pieces
    are literal slices of what the user said and each one maps to a different skill."""
    if not rt.llm.enabled or not _HAS_SEP.search(text) or len(said.split()) < 6 or _match_skill(rt, said):
        return None
    raw = rt.llm.complete(SPLIT_SYSTEM, text, "")
    m = re.search(r"\[.*\]", raw or "", re.S)
    try:
        parts = json.loads(m.group(0)) if m else []
    except ValueError:
        return None
    if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_PARALLEL or not all(isinstance(p, str) and 0 < len(p) <= 300 for p in parts):
        return None
    if not all(_norm(p) and _norm(p) in said for p in parts):
        return None
    picks = [_llm_pick(rt, p) for p in parts]
    if None in picks or len(set(picks)) != len(picks):
        return None
    return list(zip(picks, [p.strip() for p in parts]))  # type: ignore[arg-type]


# --- running tasks ---------------------------------------------------------------------------------


def _nothing(task: Task) -> str:
    if task.skill == "trip_planner":
        from chief.skills.trip_planner import parse_request

        if parse_request(task.request, None) is None:  # no "head to <place>": say what is missing
            return NO_DESTINATION
    return NOTHING


def _run_task(rt: Runtime, task: Task, catch: bool) -> Proposal | None:
    """Runs one skill with its task id on every event it emits. Safe to call from a worker thread."""
    with task_scope(task.id):
        try:
            proposal = rt.run_skill(task.skill, request=task.request)
        except Exception as exc:
            if not catch:
                raise
            rt.bus.emit("error", f"{task.skill} failed: {type(exc).__name__}", ok=False, detail={"skill": task.skill})
            task.say = f"I couldn't finish {task.skill}: {type(exc).__name__}."
            return None
        task.say = proposal.summary if proposal else _nothing(task)
        task.proposal_id = proposal.id if proposal else None
        rt.bus.emit(
            "step", f"{task.skill}: finished",
            detail={"skill": task.skill, "state": "done", "proposal_id": task.proposal_id, "read_only": bool(proposal and not proposal.actions)},
        )
        return proposal


def _run_tasks(rt: Runtime, plan: list[tuple[str, str]]) -> dict:
    tasks = [Task(rt.conversation.new_task_id(), skill, request) for skill, request in plan]
    if len(tasks) == 1:  # the ordinary case: same thread, same replies as before, errors propagate
        t = tasks[0]
        proposal = _run_task(rt, t, catch=False)
        if proposal is None:
            return _result(t.say, steps=[f"Running {t.skill}", "No changes proposed"], tasks=tasks)
        ready = "Proposal ready: approve or skip" if proposal.actions else "Read-only result ready"
        return _result(proposal.summary, proposal, [f"Running {t.skill}", ready], tasks=tasks)

    rt.bus.emit(
        "step", f"Running {len(tasks)} tasks at once",
        detail={"plan": [{"task": t.id, "skill": t.skill, "request": t.request[:200]} for t in tasks]},
    )
    with ThreadPoolExecutor(max_workers=min(len(tasks), MAX_PARALLEL), thread_name_prefix="chief-task") as pool:
        proposals = list(pool.map(lambda t: _run_task(rt, t, catch=True), tasks))
    made = [p for p in proposals if p is not None]
    rt.gate.reorder([p.id for p in made if p.actions])  # "do it" means the last one I told you about
    rt.conversation.last_proposal_ids = [p.id for p in made]  # so the note below can say what 'approve all' covers
    lines = [f"{i}. {t.skill}: {t.say}" for i, t in enumerate(tasks, 1)]
    say = f"I ran {len(tasks)} things at once.\n" + "\n".join(lines)
    note = pending_note(rt)
    if note:
        say += "\n" + note
    steps = [
        f"Split into {len(tasks)} tasks: " + ", ".join(t.skill for t in tasks),
        "Ran them at the same time",
        "Proposal ready: approve or skip" if any(p.actions for p in made) else "Results ready" if made else "No changes proposed",
    ]
    lead = next((p for p in reversed(made) if p.actions), made[-1] if made else None)
    return _result(say, lead, steps, proposals=made, tasks=tasks)


# --- approve / skip --------------------------------------------------------------------------------


def _approve(rt: Runtime, ids: list[str]) -> dict:
    changes, errors, count = 0, [], 0
    for pid in ids:
        if pid not in rt.gate.pending:
            continue
        with task_scope(rt.conversation.task_of(pid)):  # the write shows up in the lane that proposed it
            try:
                results = rt.gate.approve(pid)
            except KeyError:
                continue
        count += 1
        failed = [r for r in results if "error" in r]
        if failed:
            errors.append(failed[0]["error"])
        else:
            changes += len(results)
    if errors:
        say, steps = f"That didn't go through: {errors[0]}", ["Applying changes", "Failed"]
    elif count > 1:
        say, steps = f"Done. {changes} change(s) made across {count} proposals.", ["Applying changes", "Done"]
    else:
        say, steps = f"Done. {changes} change(s) made.", ["Applying changes", "Done"]
    note = pending_note(rt)
    return _result(say + (" " + note if note else ""), steps=steps)


def _skip(rt: Runtime, ids: list[str]) -> dict:
    for pid in ids:
        if pid in rt.gate.pending:
            with task_scope(rt.conversation.task_of(pid)):
                try:
                    rt.gate.skip(pid)
                except KeyError:
                    pass
    note = pending_note(rt)
    return _result("Okay, skipped." + (" " + note if note else ""))


def _result(
    say: str,
    proposal: Proposal | None = None,
    steps: list[str] | None = None,
    proposals: list[Proposal] | None = None,
    tasks: list[Task] | None = None,
) -> dict:
    """{"say", "proposal", "steps"} as always, plus "proposals" (every proposal made for this utterance, in
    the order spoken) and "tasks" ([{id, skill, say, proposal_id}]). `_tasks` carries the Task objects
    to handle_utterance, which removes it."""
    if proposals is None:
        proposals = [proposal] if proposal else []
    tasks = tasks or []
    return {
        "say": say, "proposal": proposal, "steps": steps or [],
        "proposals": proposals, "tasks": [t.to_json() for t in tasks], "_tasks": tasks,
    }


def handle_utterance(rt: Runtime, text: str) -> dict:
    """Returns {"say": str, "proposal": Proposal | None, "steps": [str], "proposals": [Proposal],
    "tasks": [{"id", "skill", "say", "proposal_id"}]}. Also reports to rt.bus."""
    rt.bus.emit("heard", text)
    out = _handle(rt, text)
    tasks = out.pop("_tasks")
    rt.bus.emit(
        "say", out["say"],
        detail={
            "steps": out["steps"],
            "proposal_id": out["proposal"].id if out["proposal"] else None,
            "proposal_ids": [p.id for p in out["proposals"]],
            "tasks": out["tasks"],
        },
    )
    rt.conversation.record(text, out["say"], tasks, out["proposals"])
    return out


def _handle(rt: Runtime, text: str) -> dict:
    said = _norm(text)
    if not said:
        return _result("I didn't catch that.")

    pending = list(rt.gate.pending.values())
    cmd = _command(said)

    def is_(group: set[str]) -> bool:  # the words as spoken, or without the filler around them
        return said in group or cmd in group

    if pending and is_(APPROVE):
        return _approve(rt, [pending[-1].id])  # only the most recent
    if pending and is_(APPROVE_ALL):
        last = set(rt.conversation.last_proposal_ids)
        ids = [p.id for p in pending if p.id in last]
        if not ids:
            return _result("Nothing from my last answer is waiting for approval. " + pending_note(rt))
        return _approve(rt, ids)
    if pending and is_(SKIP):
        return _skip(rt, [pending[-1].id])
    if pending and is_(SKIP_ALL):
        last = set(rt.conversation.last_proposal_ids)
        return _skip(rt, [p.id for p in pending if p.id in last] or [pending[-1].id])
    if pending and is_(UNDO):
        return _skip(rt, [pending[-1].id])  # only something still pending can be undone
    if not pending and (is_(UNDO) or is_({"never mind", "cancel"})):
        return _result("Nothing is waiting for approval. I can't undo changes that were already made.")

    follow = resolve(rt, said)
    if follow is not None:
        if follow.rerun:
            return _run_tasks(rt, [follow.rerun])
        note = pending_note(rt)
        proposals = [follow.proposal] if follow.proposal else []
        return _result(
            follow.say + (" " + note if note else ""), follow.proposal,
            ["Editing the proposal", "New proposal ready: approve or skip"] if follow.proposal else [],
            proposals=proposals,
        )

    plan = _split_by_triggers(rt, text) or _split_by_llm(rt, text, said)
    if plan is None:
        name = _route(rt, text, said)
        plan = [(name, text)] if name else None
    if plan:
        return _run_tasks(rt, plan)

    menu = ", ".join(s.name for s in rt.skills.values())
    return _result(f"I can help with: {menu}. What would you like?")

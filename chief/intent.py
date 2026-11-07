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
from datetime import timedelta

from chief.activity import task_scope
from chief.conversation import Task, pending_note, resolve
from chief.gate import SKIPPED
from chief.gate import changes as _changes
from chief.gate import made as _made
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


# The safety net for words people really use, when no trigger phrase matched and no model is connected.
# Only read-only skills, or ones whose changes still wait for approval. First match wins, so order matters.
_KEYWORDS = [
    (re.compile(r"\b(book|reserve|reservation|seats?)\b.*\b(restaurant|table|dinner|lunch|for \d|seats?)\b|\b(restaurant|table)\b.*\b(book|reserve)\b"), "booking"),
    (re.compile(r"\b(directions?|navigate|route|how far|get to|way to)\b"), "directions"),
    (re.compile(r"\b(e-?mails?|mails?|inbox|gmail|messages)\b"), "email_brief"),
    (re.compile(r"\b(calendar|meetings?|schedule|appointments?|events?)\b"), "agenda"),
    (re.compile(r"\b(restaurants?|cafes?|coffee|food|eat|hungry|pharmacy|atm|nearby|near)\b"), "nearby"),
]


def _answer_to_my_question(rt: Runtime, text: str) -> tuple[str, str] | None:
    """Chief just asked "What time...?" and you answered "7pm": finish that same request with the time.
    Only when it was the very last thing Chief said, so an old question can't catch a new message."""
    from chief.when import day_in, times_in

    last = rt.conversation.last_tasks[-1] if rt.conversation.last_tasks else None
    history = rt.conversation.history()
    if last is None or not history or "What time" not in (last.say or "") or history[-1].get("chief") != last.say:
        return None
    if last.skill not in ("booking", "calendar_edit") or not (times_in(text) or day_in(text, rt.now())):
        return None
    joiner = " " if re.match(r"\s*(at|on|tomorrow|tonight|today)\b", text, re.I) else " at "
    return last.skill, last.request.rstrip(" ?.!") + joiner + text.strip()


def _keyword_pick(rt: Runtime, said: str) -> str | None:
    for pattern, skill in _KEYWORDS:
        if skill in rt.skills and pattern.search(said):
            return skill
    return None


def _route(rt: Runtime, text: str, said: str) -> str | None:
    name = _match_skill(rt, said) or _llm_pick(rt, text)
    if not name and not rt.llm.enabled:
        name = _keyword_pick(rt, said)
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
    if task.skill == "meeting_prep":
        return "I don't see a meeting with anyone outside your team in the next day and a half, so there's nothing to prep yet."
    return NOTHING


# Which outside service a failed tool belongs to, so the person hears what broke and not a Python error.
_SERVICE = {
    "list_events": "your calendar", "find_free_slots": "your calendar", "patch_event": "your calendar",
    "create_event": "your calendar", "delete_event": "your calendar", "fetch_emails": "your email",
    "fetch_thread": "your email", "create_draft": "your email", "send_email": "your email",
    "search_web": "web search", "read_health": "your sleep data", "post_slack": "Slack",
    "find_places": "the map service", "find_location": "the map service", "get_route": "the map service",
    "list_folder": "your files", "open_folder": "your files", "open_file": "your files",
}


def failure_text(rt: Runtime, since: int, skill: str) -> str:
    """Plain words for a failure: which service, that nothing changed, what to do. Never the error's own text,
    which can hold addresses, ids or keys."""
    failed = [e.tool for e in rt.bus.since(since) if e.kind == "error" and e.tool]
    service = _SERVICE.get(failed[-1], "") if failed else ""
    if service:
        return f"I couldn't reach {service} just now, so I didn't finish that. Nothing was changed. Try again in a minute."
    return "Something went wrong on my side, so I didn't finish that. Nothing was changed. Try again, or ask another way."


def _run_task(rt: Runtime, task: Task, catch: bool = True) -> Proposal | None:
    """Runs one skill with its task id on every event it emits. Safe to call from a worker thread.
    A failure becomes a plain answer (see failure_text) unless `catch` is False."""
    started = rt.bus.last_seq
    with task_scope(task.id):
        try:
            proposal = rt.run_skill(task.skill, request=task.request)
        except Exception as exc:
            if not catch:
                raise
            rt.bus.emit("error", f"{task.skill} failed: {type(exc).__name__}", ok=False, detail={"skill": task.skill})
            task.say = failure_text(rt, started, task.skill)
            task.failed = True
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
    if len(tasks) == 1:  # the ordinary case: same thread, one answer
        t = tasks[0]
        proposal = _run_task(rt, t)
        if proposal is None:
            done = "Failed" if t.failed else "No changes proposed"
            return _result(t.say, steps=[f"Running {t.skill}", done], tasks=tasks)
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
    total, errors, count = 0, [], 0
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
            total += len(results)
    if errors:
        say, steps = f"I'm sorry, that didn't go through: {errors[0]}", ["Applying changes", "Failed"]
    elif count > 1:
        say, steps = f"All done. I made {_changes(total)} across {count} proposals. Thanks for looking them over.", ["Applying changes", "Done"]
    elif rt.gate.held:
        say, steps = f"Sending in {rt.gate.send_delay:g} seconds. Say undo to stop it.", ["Holding the email", "Sending soon"]
    else:
        say, steps = _made(total), ["Applying changes", "Done"]
        if rt.gate.last_undo:
            say += " Say undo to reverse it."
    note = pending_note(rt)
    return _result(say + (" " + note if note else ""), steps=steps)


YES_FOR = timedelta(minutes=30)  # after this a spoken "yes" no longer approves a card: tap its button instead


def _approve_offered(rt: Runtime, pending: list[Proposal]) -> dict:
    """A plain "yes" approves only the card the last answer asked about, and only while it is fresh. "sure" or a
    misheard word must never approve something older, or something shown on another screen."""
    offered = rt.conversation.offered
    target = next((p for p in pending if p.id == offered), None)
    if target is None:
        latest = pending[-1]
        return _result(f'Approve what? To approve "{_brief(latest)}", tap Approve on its card, or ask me again.')
    shown = rt.gate.since.get(target.id)
    if shown is not None and rt.now() - shown > YES_FOR:
        return _result(f'"{_brief(target)}" has been waiting a while. Tap Approve on its card if you still want it.')
    return _approve(rt, [target.id])


def _brief(p: Proposal) -> str:
    text = p.summary.splitlines()[0] if p.summary else ""
    return text if len(text) <= 90 else text[:87] + "..."


def _remember_offer(rt: Runtime, out: dict, before: set[str]) -> None:
    """Which card a "yes" to this answer would mean, and whether it offered undo.

    A card you were shown stays offered while you ask other things ("how's my week", then "what's in my inbox",
    then "do it"). Cards you were never shown in this conversation, like a heartbeat card on another screen, never are."""
    pending = list(rt.gate.pending.values())
    lead = out.get("proposal")
    say = out.get("say") or ""
    new = [p for p in pending if p.id not in before]  # e.g. a stopped email coming back as a card
    if lead is not None and lead.actions and lead.id in rt.gate.pending:
        offered = lead.id
    elif new:
        offered = new[-1].id
    elif pending and ("Waiting for your OK" in say or "'do it'" in say):
        offered = pending[-1].id  # the note at the end of the answer named it
    else:
        offered = rt.conversation.offered if rt.conversation.offered in rt.gate.pending else None
    rt.conversation.offered = offered
    rt.conversation.undo_offered = "say undo" in say.lower()


def _skip(rt: Runtime, ids: list[str]) -> dict:
    for pid in ids:
        if pid in rt.gate.pending:
            with task_scope(rt.conversation.task_of(pid)):
                try:
                    rt.gate.skip(pid)
                except KeyError:
                    pass
    note = pending_note(rt)
    return _result(SKIPPED + (" " + note if note else ""))


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
    before = set(rt.gate.pending)
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
    _remember_offer(rt, out, before)
    return out


def _handle(rt: Runtime, text: str) -> dict:
    said = _norm(text)
    if not said:
        if text.strip():  # emoji or symbols only: it heard you, it just has no words to go on
            return _result("I can only read words for now. Try: what's on today?")
        return _result("I didn't catch that.")

    pending = list(rt.gate.pending.values())
    cmd = _command(said)

    def is_(group: set[str]) -> bool:  # the words as spoken, or without the filler around them
        return said in group or cmd in group

    if pending and is_(APPROVE):
        return _approve_offered(rt, pending)
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
    if rt.gate.held and (is_(UNDO) or is_({"never mind", "cancel", "stop", "dont send it", "don't send it"})):
        return _result(rt.gate.undo() or "It has already gone.")
    if is_(UNDO) and rt.gate.last_undo and rt.conversation.undo_offered:
        return _result(rt.gate.undo() or "There is nothing to undo.")  # you were just told "say undo"
    if pending and is_(UNDO):
        return _skip(rt, [pending[-1].id])
    if is_(UNDO) and rt.gate.last_undo:
        return _result(rt.gate.undo() or "There is nothing to undo.")
    if not pending and (is_(UNDO) or is_({"never mind", "cancel"})):
        return _result("Nothing is waiting for approval, and there is nothing to undo.")

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

    answer = _answer_to_my_question(rt, text)
    if answer:
        return _run_tasks(rt, [answer])

    small = _small_talk(said)
    if small:
        return _result(small)
    say = (
        "I can help with your inbox, your calendar, reminders, email replies, meeting prep, a morning brief, places near you and directions. "
        "Try: what's on tomorrow?"
    )
    if not rt.llm.enabled:
        say += " Other questions need an AI model connected."
    return _result(say)


_HELLO = re.compile(r"^(hi|hello|hey|hiya|yo|good (morning|afternoon|evening)|morning|evening)( there| chief)?$")
_THANKS = re.compile(r"\b(thanks|thank you|thx|ty|cheers|appreciate it)\b")
_HELP = re.compile(r"\b(what can you do|what do you do|help me|^help$|how do i use you|what are you)\b")
_BYE = re.compile(r"^(bye|goodbye|good night|see you|see ya|later)( chief)?$")


def _small_talk(said: str) -> str | None:
    """The things people say to anyone: answered kindly, in plain code, without pretending to do more."""
    if _HELLO.search(said):
        return "Hi! It's good to hear from you. Ask me what's on today, what's in your inbox, or to remind you about something."
    if _THANKS.search(said):
        return "You're welcome, and thank you for letting me help. I'm here if you need anything else."
    if _BYE.search(said):
        return "Bye for now, and thanks. I'll keep an eye on things."
    if _HELP.search(said):
        return (
            "I can tell you what's on your calendar, find free time, set reminders, and move or cancel meetings. "
            "I can also sum up your inbox, tell you what someone said, and draft replies. "
            "Out and about, I find places near you (sushi near me), give directions and get a table ready for you to book. "
            "Nothing changes until you approve it. Try: remind me to call mom at 6pm. I'm always happy to help."
        )
    return None

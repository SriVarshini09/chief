"""calendar_edit: everyday changes, each one a proposal you approve.

  "remind me to call mom at 6pm"            -> a 15-minute event at 6pm
  "add lunch with Ana tomorrow at 1pm"     -> a 30-minute event (or the length you say)
  "find me 30 minutes for the gym tomorrow" -> the first free slot that fits, 8am to 8pm
  "move my 3pm to tomorrow"                 -> the same meeting, same time, the next day
  "cancel the standup"                      -> deletes it, and says it can't be undone

Plain code, no model. When it cannot tell what you mean it asks, and changes nothing.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from chief.models import Event, Proposal, ToolCall, clock
from chief.skills.base import Context, Skill
from chief.when import at, day_in, length_in, say_day, times_in

DAY_START, DAY_END = 8, 20
_STOP = {"my", "the", "a", "an", "to", "with", "and", "at", "on", "for", "meeting", "call", "event", "today", "tomorrow", "please", "me"}
_CANCEL = re.compile(r"\b(cancel|delete|remove|drop|clear)\b", re.I)
_MOVE = re.compile(r"\b(move|reschedule|push|shift|bump)\b", re.I)
_FIND = re.compile(r"\b(find|make|get)\b.*\b(time|minutes?|mins?|hours?|slot)\b", re.I)
_TIMEWORDS = re.compile(
    r"\b(today|tonight|tomorrow|day after tomorrow|on\s+\w+day|\w+day|at\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|noon|midnight|for\s+(?:half an hour|an hour|\d+(?:\.\d+)?\s*\w+)|"
    r"half an hour|an hour|\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?))\b",
    re.I,
)


def _ask(say: str, ctx: Context) -> Proposal:
    return Proposal(skill="calendar_edit", summary=say, actions=[], key=f"calendar_edit:ask:{ctx.now.isoformat()}")


def _title(text: str, lead: str) -> str:
    rest = re.sub(lead, "", text, count=1, flags=re.I)
    rest = _TIMEWORDS.sub(" ", rest)
    rest = re.sub(r"\b(to my calendar|on my calendar|in my calendar|for me)\b", " ", rest, flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.!?")
    return rest[:1].upper() + rest[1:] if rest else ""


def _upcoming(ctx: Context) -> list[Event]:
    start = datetime(ctx.now.year, ctx.now.month, ctx.now.day)
    return sorted((e for e in ctx.tools.list_events(start, start + timedelta(days=14)) if 0 < e.minutes < 24 * 60), key=lambda e: e.start)


def _which(ctx: Context, phrase: str) -> Event | None:
    """The meeting a phrase means: by its time ("my 3pm"), else by words in its title ("the standup")."""
    events = _upcoming(ctx)
    day = day_in(phrase, ctx.now)
    times = times_in(phrase)
    if times:
        hour, minute = times[0]
        for e in events:
            if (day is None or e.start.date() == day) and (e.start.hour, e.start.minute) == (hour, minute):
                return e
        return None
    words = {w for w in re.findall(r"[a-z0-9']+", phrase.lower()) if w not in _STOP and len(w) > 1}
    best, score = None, 0
    for e in events:
        if day is not None and e.start.date() != day:
            continue
        hits = len(words & set(re.findall(r"[a-z0-9']+", e.title.lower())))
        if hits > score and e.end >= ctx.now:
            best, score = e, hits
    return best


def _cancel(ctx: Context, text: str) -> Proposal:
    event = _which(ctx, _CANCEL.split(text, maxsplit=1)[-1])
    if event is None:
        return _ask("Which meeting should I cancel? Try: cancel my 3pm, or cancel the standup.", ctx)
    when = f"{say_day(event.start.date(), ctx.now)} at {clock(event.start)}"
    return Proposal(
        skill="calendar_edit",
        summary=f'Cancel "{event.title}" {when}? Deleting it can\'t be undone.',
        actions=[ToolCall("delete_event", {"event_id": event.id})],
        key=f"calendar_edit:cancel:{event.id}",
    )


def _move(ctx: Context, text: str) -> Proposal:
    body = _MOVE.split(text, maxsplit=1)[-1]
    which, _, target = body.partition(" to ")
    if not target:
        which, _, target = body.partition(" until ")
    event = _which(ctx, which)
    if event is None:
        return _ask("Which meeting should I move? Try: move my 3pm to tomorrow.", ctx)
    day, times = day_in(target, ctx.now), times_in(target)
    if day is None and not times:
        return _ask(f'When should "{event.title}" move to? Try: move it to tomorrow, or to 4pm.', ctx)
    new_day = day or event.start.date()
    start = at(new_day, times[0]) if times else at(new_day, (event.start.hour, event.start.minute))
    end = start + (event.end - event.start)
    return Proposal(
        skill="calendar_edit",
        summary=f'Move "{event.title}" to {say_day(new_day, ctx.now)} at {clock(start)}?',
        actions=[ToolCall("patch_event", {"event_id": event.id, "fields": {"start": start.isoformat(), "end": end.isoformat()}})],
        key=f"calendar_edit:move:{event.id}:{start.isoformat()}",
    )


def _find(ctx: Context, text: str) -> Proposal:
    minutes = length_in(text) or 30
    purpose = re.search(r"\bfor\s+(?:the\s+|a\s+|an\s+|my\s+)?([a-z][a-z' -]{1,40}?)(?=\s+(?:today|tomorrow|on|at|this|next)\b|[,.!?]|$)", text, re.I)
    title = purpose.group(1).strip().capitalize() if purpose else "Focus time"
    day = day_in(text, ctx.now) or ctx.now.date()
    opens = at(day, (DAY_START, 0))
    if day == ctx.now.date():
        opens = max(opens, (ctx.now + timedelta(minutes=15)).replace(second=0, microsecond=0))
    closes = at(day, (DAY_END, 0))
    slots = ctx.tools.find_free_slots(opens, closes, minutes) if opens < closes else []
    if not slots:
        return _ask(f"I couldn't find {minutes} free minutes {say_day(day, ctx.now)} between 8am and 8pm.", ctx)
    start = slots[0][0]
    end = start + timedelta(minutes=minutes)
    return Proposal(
        skill="calendar_edit",
        summary=f'You\'re free {say_day(day, ctx.now)} at {clock(start)}. Add "{title}" from {clock(start)} to {clock(end)}?',
        actions=[ToolCall("create_event", {"title": title, "start": start.isoformat(), "end": end.isoformat()})],
        key=f"calendar_edit:find:{start.isoformat()}:{title}",
    )


def _add(ctx: Context, text: str) -> Proposal:
    reminder = re.search(r"\bremind me\b", text, re.I) is not None
    lead = r"^.*?\bremind me\s+(?:to\s+|about\s+)?" if reminder else r"^.*?\b(?:add|schedule|book|put|block|set up)\s+(?:an?\s+|some\s+)?(?:event\s+|meeting\s+)?"
    title = _title(text, lead)
    times = times_in(text)
    if not title:
        return _ask("What should I put on your calendar? Try: remind me to call mom at 6pm.", ctx)
    if not times:
        return _ask(f'What time should I put "{title}" at? Try adding a time, like 6pm.', ctx)
    day = day_in(text, ctx.now) or ctx.now.date()
    start = at(day, times[0])
    if day_in(text, ctx.now) is None and start < ctx.now:
        start += timedelta(days=1)                          # "at 9am" said in the afternoon means tomorrow
    minutes = length_in(text) or (15 if reminder else 30)
    end = start + timedelta(minutes=minutes)
    what = "Remind you to" if reminder else "Add"
    shown = title[0].lower() + title[1:] if reminder else f'"{title}"'
    return Proposal(
        skill="calendar_edit",
        summary=f"{what} {shown} {say_day(start.date(), ctx.now)} at {clock(start)}?",
        actions=[ToolCall("create_event", {"title": title, "start": start.isoformat(), "end": end.isoformat()})],
        key=f"calendar_edit:add:{start.isoformat()}:{title}",
    )


def run(ctx: Context) -> Proposal:
    text = ctx.request.strip()
    if _CANCEL.search(text):
        return _cancel(ctx, text)
    if _MOVE.search(text):
        return _move(ctx, text)
    if _FIND.search(text):
        return _find(ctx, text)
    return _add(ctx, text)


SKILL = Skill(
    label="Your calendar",
    name="calendar_edit",
    description="Reminders, new events, finding free time, moving and cancelling meetings. Every change waits for approval.",
    triggers=[
        "remind me", "add a meeting", "add an event", "add lunch", "add a call", "to my calendar",
        "schedule a", "book a", "find me time", "find time", "make time", "minutes for", "hour for", "an hour for",
        "move my", "move the", "reschedule", "push my", "push the", "cancel my", "cancel the", "delete my", "delete the",
    ],
    allowed_tools={"list_events", "find_free_slots"},
    propose_tools={"create_event", "patch_event", "delete_event"},
    run=run,
    heartbeat=False,
)

"""agenda: "what's on my calendar tomorrow?", "when is my first meeting?", "what's next?", "am I free at 3pm?".

Read-only. Built in plain code from the calendar, so it needs no model.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from chief.models import Event, Proposal, clock
from chief.skills.base import Context, Skill
from chief.when import at, day_in, say_day, times_in


def _timed(events: list[Event]) -> list[Event]:
    return sorted((e for e in events if 0 < e.minutes < 24 * 60), key=lambda e: e.start)


def _list(events: list[Event]) -> str:
    named = [f"{e.title} at {clock(e.start)}" for e in events[:4]]
    if len(events) > 4:
        named.append(f"{len(events) - 4} more")
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]


def run(ctx: Context) -> Proposal:
    text, now = ctx.request.lower(), ctx.now
    asked_day = day_in(text, now)
    day = asked_day or now.date()
    start = datetime(day.year, day.month, day.day)
    events = _timed(ctx.tools.list_events(start, start + timedelta(days=1)))
    when = say_day(day, now)

    if "next" in text and asked_day is None:
        ahead = _timed(ctx.tools.list_events(now, now + timedelta(days=7)))
        upcoming = [e for e in ahead if e.start >= now]
        say = (f"Your next meeting is {upcoming[0].title}, {say_day(upcoming[0].start.date(), now)} at {clock(upcoming[0].start)}."
               if upcoming else "You have no meetings in the next seven days.")
    elif "free" in text or "busy" in text:
        times = times_in(text)
        if not times:
            free = len(events) == 0
            say = f"Yes, your calendar is clear {when}." if free else f"You have {len(events)} meeting{'s' * (len(events) != 1)} {when}: {_list(events)}."
        else:
            moment = at(day, times[0])
            clash = next((e for e in events if e.start <= moment < e.end), None)
            say = (f"No, you have {clash.title} then, from {clock(clash.start)} to {clock(clash.end)}."
                   if clash else f"Yes, you're free at {clock(moment)} {when}.")
    elif "first" in text:
        say = f"Your first meeting {when} is {events[0].title} at {clock(events[0].start)}." if events else f"You have no meetings {when}."
    elif "last" in text:
        say = f"Your last meeting {when} is {events[-1].title}, ending at {clock(events[-1].end)}." if events else f"You have no meetings {when}."
    elif events:
        count = f"{len(events)} meeting{'s' * (len(events) != 1)}"
        say = f"{when[0].upper() + when[1:]} you have {count}: {_list(events)}."
    else:
        say = f"Nothing on your calendar {when}."
    return Proposal(skill="agenda", summary=say, actions=[], key=f"agenda:{now.isoformat()}:{text[:40]}")


SKILL = Skill(
    label="Your calendar",
    name="agenda",
    description="Read-only answers about the calendar: what's on a day, the first or next meeting, whether you're free.",
    triggers=[
        "on my calendar", "calendar today", "calendar tomorrow", "first meeting", "when is my next", "what's my next",
        "last meeting", "my schedule", "meetings today", "meetings tomorrow", "what's on tomorrow", "agenda",
        "am i free", "am i busy", "any meetings", "what's next", "do i have meetings",
    ],
    allowed_tools={"list_events"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

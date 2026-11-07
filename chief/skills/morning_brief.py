"""Morning brief: one short read of today. Today's meetings, mail that needs an answer, and deadlines.

Built in plain code with no model, so email text cannot steer it. A read-only Proposal, like
`email_brief`. It does not run on the heartbeat: `chief/telegram_app.py` sends it once each morning,
and you can ask for it at any time.
"""

from __future__ import annotations

from datetime import timedelta

from chief.models import Event, Proposal, clock
from chief.skills.base import Context, Skill
from chief.skills.email_brief import classify, item_line, recent_unread


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _day_line(events: list[Event]) -> str:
    if not events:
        return "No meetings today."
    first, last = events[0], max(events, key=lambda e: e.end)
    return f"{_plural(len(events), 'meeting')}. First: {first.title} at {clock(first.start)}. Done by {clock(last.end)}."


def run(ctx: Context) -> Proposal:
    start = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = ctx.tools.list_events(start, start + timedelta(days=1))
    timed = sorted((e for e in events if 0 < e.minutes < 24 * 60), key=lambda e: e.start)
    items = [classify(m) for m in recent_unread(ctx)]

    hour = ctx.now.hour
    hello = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    lines = [f"{hello}. Here's your {ctx.now:%A}.", _day_line(timed)]
    for label, kind in (("To answer", "reply"), ("Deadlines", "deadline")):
        found = [i for i in items if i.kind == kind]
        if found:
            lines.append(f"{label}: " + "; ".join(item_line(i) for i in found))
    flagged = sum(1 for i in items if i.kind == "suspicious")
    if flagged:
        lines.append(f"I ignored {_plural(flagged, 'email')} that tried to give me orders.")
    return Proposal(skill="morning_brief", summary="\n".join(lines), actions=[], key=f"morning_brief:{ctx.now.date()}")


SKILL = Skill(
    label="Morning brief",
    name="morning_brief",
    description="Read-only summary of today: meetings, mail that needs an answer, and deadlines.",
    triggers=["morning brief", "daily brief", "what's my day", "how's my day", "plan for today", "what's on today"],
    allowed_tools={"list_events", "fetch_emails"},
    propose_tools=set(),
    run=run,
    heartbeat=False,  # sent once each morning instead: see chief/telegram_app.py
)

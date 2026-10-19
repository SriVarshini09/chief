"""audit: does a heavy meeting day cost you sleep? If so, protect the next heavy day.

All findings are computed deterministically from calendar + sleep data; the model (if any)
only phrases the summary. The fix is a proposal: nothing changes until you approve.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from chief.models import Event, Proposal, ToolCall
from chief.skills.base import Context, Skill

HEAVY_MEETINGS = 5  # meetings in a day that count as heavy
SHORT_SLEEP_H = 6.0
MIN_EVIDENCE = 3  # heavy days with a recorded night before we say anything
MIN_RATE = 0.6  # share of those nights that were short
FOCUS_MINUTES = 90
ASYNC_WORDS = ("sync", "status", "update")


def _by_day(events: list[Event]) -> dict[date, list[Event]]:
    days: dict[date, list[Event]] = defaultdict(list)
    for e in events:
        days[e.start.date()].append(e)
    return days


def run(ctx: Context) -> Proposal | None:
    now: datetime = ctx.now
    past = _by_day(ctx.tools.list_events(now - timedelta(days=14), now))
    sleep = {s.day: s.hours for s in ctx.tools.read_health(14)}

    heavy = [d for d, evs in past.items() if len(evs) >= HEAVY_MEETINGS]
    measured = [d for d in heavy if d + timedelta(days=1) in sleep]
    short = [d for d in measured if sleep[d + timedelta(days=1)] < SHORT_SLEEP_H]
    if len(measured) < MIN_EVIDENCE or len(short) / len(measured) < MIN_RATE:
        return None  # nothing worth saying

    upcoming = _by_day(ctx.tools.list_events(now, now + timedelta(days=7)))
    target = next((d for d in sorted(upcoming) if len(upcoming[d]) >= HEAVY_MEETINGS), None)
    if target is None:
        return None

    events = sorted(upcoming[target], key=lambda e: e.start)
    asyncable = [e for e in events if any(w in e.title.lower() for w in ASYNC_WORDS)][:2]
    walk = next((e for e in events if "1:1" in e.title), None)

    actions: list[ToolCall] = []
    for e in asyncable:
        actions.append(
            ToolCall(
                "patch_event",
                {
                    "event_id": e.id,
                    "fields": {
                        "summary": f"[Async] {e.title}",
                        "description": "Chief suggests handling this as a written update instead of a live call.",
                    },
                },
            )
        )
    if walk:
        actions.append(
            ToolCall(
                "patch_event",
                {"event_id": walk.id, "fields": {"summary": f"Walk: {walk.title}", "description": "Walking call, no screen."}},
            )
        )
    if len(asyncable) == 2 and asyncable[0].end == asyncable[1].start:
        start = asyncable[0].start
        actions.append(
            ToolCall(
                "create_event",
                {
                    "title": "Focus block (protected by Chief)",
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=FOCUS_MINUTES)).isoformat(),
                    "description": "Deep work. Created by Chief after a sleep and meeting audit.",
                },
            )
        )
    if not actions:
        return None

    day_name = target.strftime("%A")
    fallback = (
        f"You slept under {SHORT_SLEEP_H:g} hours after {len(short)} of your last {len(measured)} heavy meeting days. "
        f"{day_name} has {len(events)} meetings. I can mark {len(asyncable)} as async, "
        f"{'make one a walking call, ' if walk else ''}and protect a {FOCUS_MINUTES}-minute focus block. Approve?"
    )
    summary = ctx.llm.complete(
        "You are Chief, a dry, concise personal agent. Rewrite in at most three short sentences, keep every number.",
        fallback,
        fallback,
    )
    return Proposal(skill="audit", summary=summary, actions=actions, key=f"audit:{target.isoformat()}")


SKILL = Skill(
    name="audit",
    description="Finds whether heavy meeting days cost you sleep, then proposes a lighter next heavy day.",
    triggers=["how's my week", "how is my week", "why am i so tired", "audit"],
    allowed_tools={"list_events", "read_health"},
    propose_tools={"patch_event", "create_event"},
    run=run,
)

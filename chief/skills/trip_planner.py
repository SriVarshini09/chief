"""trip_planner: "head to Palo Alto after this, find me a Caltrain after 6 and a boba place on the way".

Train times are NEVER invented: a time is used only if it appears in a search_web excerpt from a pinned
source (caltrain.com / 511.org), and the source URL always travels with it. If no usable time is found the
skill says so and proposes nothing. The only write is a create_event, and it waits for approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from chief.models import Event, Proposal, ToolCall
from chief.skills.base import Context, Skill

TRUSTED_HOSTS = ("caltrain.com", "511.org")
MOCK_URL_PREFIX = "https://example.com/mock"  # mock mode only: labelled MOCK data, never a real schedule
FALLBACK_TRIP_MIN = 60  # event length when the source gives no arrival time (stated on the event)
BUFFER_MIN = 10  # gap between "this" meeting ending and leaving

_DEST = re.compile(
    r"\b(?:head(?:ing)?|go(?:ing)?|get(?:ting)?|travel(?:l?ing)?|commute|commuting)\s+(?:out\s+|down\s+|back\s+)?to\s+"
    r"([a-z][a-z .'-]*?)(?=\s+(?:after|before|by|then|and|when|from|on|at|tomorrow|tonight|today)\b|[,.!?;]|$)",
    re.I,
)
_AFTER = re.compile(r"\bafter\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![a-z])", re.I)
_TIME = r"(\d{1,2}):(\d{2})\s*([ap])(?:\.?m\.?)?(?![a-z])"
_T = re.compile(_TIME, re.I)
_PAIR = re.compile(rf"(?:depart\w*|leav\w*)\D{{0,30}}?{_TIME}(?:\D{{0,40}}?arriv\w*\D{{0,30}}?{_TIME})?", re.I)


@dataclass
class Train:
    start: datetime
    end: datetime | None
    url: str
    excerpt: str


def _clock(h: str, m: str, ap: str) -> time:
    return time((int(h) % 12) + (12 if ap.lower() == "p" else 0), int(m))


def _trusted(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return url.startswith(MOCK_URL_PREFIX) or any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)


def _times_in(text: str) -> set[time]:
    return {_clock(*m.groups()) for m in _T.finditer(text)}


def parse_request(text: str, day: date) -> tuple[str, time | None, bool] | None:
    """(destination, 'after' clock time, wants to leave after the current/next meeting) or None."""
    d = _DEST.search(text)
    if not d:
        return None
    dest = " ".join(d.group(1).split()).title()
    after = None
    a = _AFTER.search(text)
    if a:
        hour, minute, mer = int(a.group(1)), int(a.group(2) or 0), (a.group(3) or "").lower().replace(".", "")
        if mer == "pm" and hour < 12:
            hour += 12
        elif mer == "am" and hour == 12:
            hour = 0
        elif not mer and 1 <= hour <= 9:
            hour += 12  # "after 6" means evening
        if 0 <= hour < 24 and 0 <= minute < 60:
            after = time(hour, minute)
    return dest, after, "after this" in text.lower()


def _trains(results: list[dict], day: date, after: datetime) -> list[Train]:
    """Departures at or after `after`, read straight from trusted excerpts. Nothing is inferred."""
    found: dict[datetime, Train] = {}
    for r in results:
        url = r.get("url") or ""
        if not _trusted(url):
            continue
        for ex in r.get("excerpts") or []:
            pairs = [(m.groups()[:3], m.groups()[3:]) for m in _PAIR.finditer(ex)]
            if not pairs:  # no "departs/leaves" wording: fall back to every time in the excerpt, no arrival
                pairs = [(m.groups(), (None, None, None)) for m in _T.finditer(ex)]
            for dep, arr in pairs:
                start = datetime.combine(day, _clock(*dep))
                end = datetime.combine(day, _clock(*arr)) if arr[0] else None
                if end is not None and end <= start:
                    end = None
                if start >= after:
                    found.setdefault(start, Train(start, end, url, ex))
    return [found[k] for k in sorted(found)]


def _fits(ctx: Context, day_events: list[Event], free: list[tuple[datetime, datetime]], t: Train) -> bool:
    end = t.end or t.start + timedelta(minutes=FALLBACK_TRIP_MIN)
    if any(e.start < end and e.end > t.start for e in day_events):
        return False
    return any(s <= t.start and end <= e for s, e in free)


def _fmt(t: time | datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def run(ctx: Context) -> Proposal | None:
    day = ctx.now.date() + (timedelta(days=1) if "tomorrow" in ctx.request.lower() else timedelta())
    parsed = parse_request(ctx.request, day)
    if parsed is None:
        return None  # nothing asked, nothing to plan
    dest, after_clock, after_this = parsed

    midnight = datetime.combine(day + timedelta(days=1), time.min)
    day_events = ctx.tools.list_events(datetime.combine(day, time.min), midnight)
    leave_after = datetime.combine(day, after_clock) if after_clock else max(ctx.now, datetime.combine(day, time(8)))
    if after_this:
        this = next((e for e in day_events if e.end > ctx.now), None)  # in progress, else the next one
        if this is not None:
            leave_after = max(leave_after, this.end + timedelta(minutes=BUFFER_MIN))

    train_results = ctx.tools.search_web(
        f"Find Caltrain departures after {_fmt(leave_after)} on {day:%A %B %d} to {dest}. "
        "Use only official sources: caltrain.com and 511.org (transit.511.org). Quote the departure and arrival times exactly.",
        [f"Caltrain schedule to {dest} after {_fmt(leave_after)} site:caltrain.com", f"511 Caltrain trip to {dest} site:511.org"],
        5,
    )
    boba = next(
        (r for r in ctx.tools.search_web(
            f"Find a well-reviewed boba tea shop near the Caltrain station in {dest}, easy to stop at on the way.",
            [f"best boba tea near {dest} Caltrain station"], 3,
        ) if r.get("url")),
        None,
    )
    boba_line = f"Boba on the way: {boba.get('title', 'a boba shop')} ({boba['url']})." if boba else "I found no boba place to suggest."
    seen_urls = [r["url"] for r in train_results if r.get("url") and _trusted(r["url"])]

    free = ctx.tools.find_free_slots(leave_after, midnight, 30)
    options = [t for t in _trains(train_results, day, leave_after) if _fits(ctx, day_events, free, t)]
    sources = list(dict.fromkeys(seen_urls + ([boba["url"]] if boba else [])))
    key = f"trip_planner:{day.isoformat()}:{dest.lower()}:{leave_after:%H%M}"

    if not options:
        text = (
            f"I couldn't find a Caltrain to {dest} after {_fmt(leave_after)} in caltrain.com or 511.org results, "
            f"so I'm not guessing times. Check caltrain.com or 511.org directly. {boba_line}"
        )
        return Proposal(skill="trip_planner", summary=text, actions=[], key=key + ":none", sources=sources)

    t = options[0]
    end = t.end or t.start + timedelta(minutes=FALLBACK_TRIP_MIN)
    arrives = f", arrives {_fmt(t.end)}" if t.end else ""
    note = "" if t.end else " Arrival time is not in the source, so the event length is a placeholder."
    fallback = (
        f"Caltrain to {dest}: the first train after {_fmt(leave_after)} that fits your calendar leaves {_fmt(t.start)}{arrives}, "
        f"per {t.url}.{note} {boba_line} Add it to your calendar? Approve to create the event."
    )
    summary = ctx.llm.complete(
        "You are Chief, a dry, concise personal agent. Rewrite in at most three short sentences. Keep every time, "
        "place and URL exactly as given. Never add a time, place or fact that is not in the text.",
        fallback,
        fallback,
    )
    if not (_times_in(summary) <= _times_in(fallback) and t.url in summary):
        summary = fallback  # the model must not introduce a train time or drop the source

    event = ToolCall(
        "create_event",
        {
            "title": f"Caltrain to {dest}",
            "start": t.start.isoformat(),
            "end": end.isoformat(),
            "description": f"Source: {t.url}\nExcerpt: {t.excerpt}\n{boba_line}{note}",
        },
    )
    return Proposal(skill="trip_planner", summary=summary, actions=[event], key=key, sources=sources)


SKILL = Skill(
    name="trip_planner",
    description="Plans a Caltrain trip from your calendar and a pinned-source search, plus a boba stop. Never invents train times.",
    triggers=["caltrain", "head to palo alto", "heading to palo alto", "boba", "plan my trip", "plan a trip"],
    allowed_tools={"list_events", "find_free_slots", "search_web"},
    propose_tools={"create_event"},
    run=run,
    heartbeat=False,  # needs a spoken request: it only runs when asked
)

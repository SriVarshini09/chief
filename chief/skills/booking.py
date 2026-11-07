"""booking: "book a table at the first one for 2 at 7pm", "reserve Sakura for four tomorrow at 8", "book tickets for Dune".

Chief gets everything ready: the place, the time, how many of you, its address, phone and booking page. It never
books, pays or signs in for you: you tap Reserve (or Call) and finish there, payment included. If you approve, the
only thing Chief does itself is put the plan on your calendar, through the approval gate like every other change.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from chief.models import Proposal, ToolCall, clock
from chief.places import CREDIT, Spot, directions_link, search_link
from chief.skills.base import Context, Skill
from chief.skills.nearby import pick
from chief.when import at, day_in, say_day, times_in

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_PARTY = re.compile(
    r"\b(?:for|party of|table of|of us|we are|we're)\s+(?P<n>\d{1,2}|" + "|".join(_WORDS) + r")\b(?!\s*(?::|am\b|pm\b|a\.m|p\.m|o'?clock|minutes|mins|hours))"
    r"|\b(?P<m>\d{1,2}|" + "|".join(_WORDS) + r")\s+(?:people|persons|guests|adults|of us|pax|seats?|members|ppl)\b",
    re.I,
)
# "book 2 seats for restaurant for Lemon Crab Hayward": the name after "for" (or "at") that is not a number, a time or a meal
_FOR_NAME = re.compile(
    r"\b(?:for|at)\s+(?:the\s+)?(?:restaurant\s+|place\s+)?(?:called\s+|named\s+)?"
    r"(?!\d|for\b|a\b|an\b|me\b|us\b|tonight|today|tomorrow|dinner|lunch|breakfast|brunch|restaurant\b|place\b|noon|midnight|"
    + "|".join(rf"{w}\b" for w in _WORDS) +
    r")(?P<n>[a-z][^,.;!?]*?)(?=\s+(?:for|at|on|tomorrow|tonight|today|this|next|around|by)\b|[,.;!?]|$)",
    re.I,
)
_NAMED = re.compile(
    r"\b(?:at|in|from|reserve|book)\s+(?!a\b|an\b|the first|the second|the third|number|no\.?\s*\d|#|\d|noon|midnight|table|tickets?|it\b|there\b|that\b)"
    r"(?P<n>[^,.;!?]+?)(?=\s+(?:for|at|on|tomorrow|tonight|today|this|next|around|by)\b|[,.;!?]|$)",
    re.I,
)
_TICKETS = re.compile(r"\btickets?\b(?:\s+(?:for|to)\s+(?P<what>[^,.;!?]+?))?(?=\s+(?:at|on|tomorrow|tonight|today|this|next)\b|[,.;!?]|$)", re.I)
NOT_ME = "I can't make the booking or pay for you"


def _party(text: str) -> int | None:
    for m in _PARTY.finditer(text):
        raw = (m.group("n") or m.group("m") or "").lower()
        n = _WORDS.get(raw) or (int(raw) if raw.isdigit() else 0)
        if 1 <= n <= 30:
            return n
    return None


def _when(ctx: Context, text: str):
    times = times_in(text)
    if not times:
        return None
    day = day_in(text, ctx.now)
    hour, minute = times[0]
    said_morning = re.search(r"\d\s*a\.?m\b|\bmorning\b|\bbreakfast\b|\bbrunch\b", text, re.I)
    if 8 <= hour <= 10 and not said_morning:  # "a table at 8" means the evening, unless you said am or breakfast
        hour += 12
    start = at(day or ctx.now.date(), (hour, minute))
    if day is None and start < ctx.now:
        start += timedelta(days=1)  # "at 7pm" said at 9pm means tomorrow
    return start


def _meal(hour: int) -> str:
    return "Breakfast" if hour < 11 else "Lunch" if hour < 16 else "Dinner"


def _id() -> str:
    return f"booking:{uuid.uuid4().hex}"


def _restaurant(ctx: Context, text: str) -> Spot | None:
    chosen = pick(ctx, text)
    if chosen:
        return chosen
    m = _NAMED.search(text)
    found = [f.group("n").strip() for f in _FOR_NAME.finditer(text)]
    if m:
        name = m.group("n").strip()
    elif found:
        name = found[-1]  # the last one is the most specific: "for restaurant for Lemon Crab Hayward"
    else:
        return None
    here = ctx.where.here(ctx.now)
    return ctx.tools.find_location(name, here.lat if here else None, here.lon if here else None)


def _links(spot: Spot) -> list[dict]:
    reserve = spot.website or search_link(f"{spot.name} {spot.address} book a table".strip())
    links = [{"label": "Reserve" if spot.website else "Find a booking page", "url": reserve}]
    if spot.phone:
        links.append({"label": "Call", "url": f"tel:{spot.phone}"})
    links.append({"label": "Directions", "url": directions_link(spot.lat, spot.lon)})
    return links


def _tickets(ctx: Context, text: str, m: re.Match) -> Proposal:
    what = (m.group("what") or "").strip()
    where = ctx.where.here(ctx.now)
    near = f" near {where.label}" if where and where.label and where.source == "said" else ""
    if not what:
        return Proposal("booking", "Tickets for what? Try: book tickets for Dune tomorrow at 7pm.", [], _id())
    links = [{"label": "Find tickets", "url": search_link(f"{what} tickets{near}")}]
    start = _when(ctx, text)
    if start is None:
        say = (f"{NOT_ME}, but tap Find tickets to pick seats and pay for {what}. "
               "Tell me the time you choose and I'll put it on your calendar.")
        return Proposal("booking", say, [], _id(), links=links)
    title = what[:1].upper() + what[1:]
    say = (f"{NOT_ME}: tap Find tickets to pick your seats and pay for {what}. "
           f"Approve and I'll put {title} on your calendar {say_day(start.date(), ctx.now)} at {clock(start)}.")
    action = ToolCall("create_event", {
        "title": title, "start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat(),
        "description": "Tickets: buy them yourself on the booking page. Chief did not buy them.",
    })
    return Proposal("booking", say, [action], _id(), links=links)


def run(ctx: Context) -> Proposal:
    text = ctx.request.strip()
    m = _TICKETS.search(text)
    if m and not re.search(r"\btable\b", text, re.I):
        return _tickets(ctx, text, m)

    spot = _restaurant(ctx, text)
    if spot is None:
        say = ("Which place should I get ready? Ask me for places first, like sushi near me, "
               "then say: book a table at the first one for 2 at 7pm.")
        return Proposal("booking", say, [], _id())
    ctx.where.chosen = spot
    party, start = _party(text), _when(ctx, text)
    links = _links(spot)
    if start is None:
        if party:
            say = f"Lovely, {spot.name} for {party}. What time would you like? Try: 7pm tonight, or tomorrow at 8."
        else:
            say = f"What time should I plan {spot.name} for, and for how many? Try: book it for 2 at 7pm."
        return Proposal("booking", say, [], _id(), links=links, sources=[CREDIT])

    people = party or 2
    guess = "" if party else " (say another number if it's not 2)"
    finish = "tap Reserve to finish on their page" if spot.website else "tap Find a booking page to finish it"
    if spot.phone:
        finish += ", or Call them"
    say = (f"Ready for {spot.name}: a table for {people}{guess}, {say_day(start.date(), ctx.now)} at {clock(start)}. "
           f"{NOT_ME}, so {finish}: you pick the table and pay there. Approve and I'll put it on your calendar.")
    details = [f"Table for {people}."]
    if spot.address:
        details.append(f"Address: {spot.address}.")
    if spot.phone:
        details.append(f"Phone: {spot.phone}.")
    details.append(f"Booking: {links[0]['url']}")
    details.append("Not booked by Chief: confirm it on their page or by phone.")
    action = ToolCall("create_event", {
        "title": f"{_meal(start.hour)} at {spot.name}", "start": start.isoformat(),
        "end": (start + timedelta(minutes=90)).isoformat(), "description": " ".join(details),
    })
    if spot.sample:
        say += " (Sample place: switch to live mode for real ones.)"
    return Proposal("booking", say, [action], _id(), links=links, sources=[CREDIT])


SKILL = Skill(
    label="Booking",
    name="booking",
    description="Gets a restaurant table or event tickets ready: place, time, party size and the booking page. You finish and pay.",
    triggers=[
        "book a table", "book table", "reserve a table", "table for", "make a reservation", "reservation at",
        "reservation for", "book tickets", "book a ticket", "buy tickets", "get tickets", "book it for", "reserve it",
        "book the first", "book the second", "book number", "reserve the first", "book that place",
        "seats for", "seats at", "seat for", "book seats", "reserve seats", "a reservation", "booking for", "booking at",
        "book 2", "book 3", "book 4", "book 5", "book 6", "book two", "book three", "book four",
    ],
    allowed_tools={"find_places", "find_location"},
    propose_tools={"create_event"},
    run=run,
    heartbeat=False,
)

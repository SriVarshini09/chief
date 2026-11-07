"""nearby: "sushi near me", "any coffee around here?", "I'm at Union Square, find a pharmacy".

Read-only. Uses where you are (shared from the page or Telegram, or said: "I'm at ..."), looks the places up on
OpenStreetMap, and reads out the closest with distance, walking time and whether they're open now. The list is
remembered for a follow-up: "directions to the first one", "book a table at number 2 for 7pm".

The helpers here (where you are, "the first one", a place's card) are shared with the directions and booking skills.
"""

from __future__ import annotations

import re
import uuid

from chief.models import Proposal
from chief.places import CREDIT, Here, Spot, directions_link, open_now, say_distance, say_minutes, walk_minutes, want_from
from chief.skills.base import Context, Skill

# ---- where you are ------------------------------------------------------------------------------------

_VAGUE = {"my location", "my current location", "here", "home", "work", "the office", "office", "my place", "my desk", "the moment"}
_IM_AT = re.compile(
    r"\b(?:i'?m|i am|im|we'?re|we are)\s+(?:at|in|near|by|around)\s+(?P<place>.+?)"
    r"(?=\s*[.;!?]|,?\s+(?:and|so|now|right now|find|show|any|where|what|get|look|search|can|could|please|is|are|how|book|take|near|nearby|around)\b|$)",
    re.I,
)
_SEARCHY = re.compile(r"\b(near|nearby|around|find|show|any|places?|food|eat)\b", re.I)


def _place_and_rest(said: str) -> tuple[str, str]:
    """ "Union Square, San Francisco" is one place; "Union Square, sushi" is a place and a search."""
    parts = [p.strip() for p in said.split(",")]
    keep = [parts[0]]
    for i, part in enumerate(parts[1:], 1):
        if not part or want_from(part).filters or _SEARCHY.search(part):
            return ", ".join(keep), ", ".join(parts[i:])
        keep.append(part)
    return ", ".join(keep), ""
_NEAR = re.compile(
    r"\b(?:near|around|close to|next to|in)\s+(?!me\b|here\b|us\b|my\b|walking\b|the area\b|this area\b|town\b|the city\b|a\b|an\b)"
    r"(?P<place>[^,.;!?]+?)(?=\s*[,.;!?]|\s+(?:for|that|which|open|with|tonight|today|tomorrow)\b|$)",
    re.I,
)
ASK_WHERE = (
    "Can I use your live location? Then I can check what's near you. Tap Allow (in Telegram: Share my location), "
    "or just tell me where you are, like: I'm at Union Square."
)


def ask_for_location(ctx: Context, skill: str, extra: str = "") -> Proposal:
    return Proposal(skill=skill, summary=(extra + " " if extra else "") + ASK_WHERE, actions=[], key=f"{skill}:{uuid.uuid4().hex}", needs="location")


def here_label(h: Here) -> str:
    return "you" if h.source in ("browser", "telegram") or not h.label else h.label


def locate(ctx: Context, text: str) -> tuple[Here | None, str, str, str | None]:
    """Where to search from: (centre, what you said with the place taken out, a note to say first, a problem).

    "I'm at X" moves your location to X. "near X" searches around X just this once."""
    here = ctx.where.here(ctx.now)
    m = _IM_AT.search(text)
    if m:
        place, extra = _place_and_rest(m.group("place").strip(" ,.'\""))
        rest = (text[: m.start()] + " " + extra + " " + text[m.end():]).strip()
        if place.lower() in _VAGUE or not place:
            return here, rest, "", None
        spot = ctx.tools.find_location(place, here.lat if here else None, here.lon if here else None)
        if spot is None:
            return None, rest, "", f"I couldn't find {place} on the map. Try a street or an area name, or share your location."
        label = spot.name + (f", {spot.address}" if spot.address and spot.address.lower() not in spot.name.lower() else "")
        h = ctx.where.set(spot.lat, spot.lon, label=label, source="said", now=ctx.now)
        return h, rest, f"Got it, you're at {label}.", None
    m = _NEAR.search(text)
    if m and not re.search(r"\b(?:near|around|close to)\s+(?:me|here|us)\b", text, re.I):
        place = m.group("place").strip(" ,.'\"")
        if place and want_from(place).filters == [] and not place.lower().startswith(("the first", "number", "#")):
            spot = ctx.tools.find_location(place, here.lat if here else None, here.lon if here else None)
            if spot is None:
                return None, text, "", f"I couldn't find {place} on the map. Try a street or an area name."
            return Here(spot.lat, spot.lon, spot.name, "said", ctx.now), text[: m.start()] + " " + text[m.end():], "", None
    return here, text, "", None


# ---- "the first one", "number 2", "Sakura" ------------------------------------------------------------

_ORDINALS = {
    "first": 0, "1st": 0, "closest": 0, "nearest": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "sixth": 5, "6th": 5, "last": -1,
}
_NUMBER = re.compile(r"(?:\bnumber\s*|\bno\.?\s*|#|\boption\s*)(\d{1,2})\b", re.I)
_THERE = re.compile(r"\b(there|that place|that one|this place|it)\b", re.I)
_SKIP_WORDS = {"the", "a", "an", "at", "to", "for", "and", "of", "on", "in", "restaurant", "cafe", "bar", "sushi", "kitchen", "place", "table", "book", "me", "my"}


def pick(ctx: Context, text: str) -> Spot | None:
    """A place from the last list or the one we were just talking about, or None."""
    spots = ctx.where.spots
    low = text.lower()
    if spots:
        m = _NUMBER.search(low)
        if m and 1 <= int(m.group(1)) <= len(spots):
            return spots[int(m.group(1)) - 1]
        for word, i in _ORDINALS.items():
            # "first", "the second one"; but "closest", "nearest" and "last" only as "the closest one"
            plain = word not in ("closest", "nearest", "last")
            if re.search(rf"\b{word}\b" if plain else rf"\bthe\s+{word}\s+(?:one|place|option)\b", low) and i < len(spots):
                return spots[i]
        words = {w for w in re.findall(r"[a-z0-9']+", low) if w not in _SKIP_WORDS and len(w) > 2}
        best, score = None, 0
        for s in spots:
            hits = len(words & set(re.findall(r"[a-z0-9']+", s.name.lower())))
            if hits > score:
                best, score = s, hits
        if best:
            return best
    if ctx.where.chosen and _THERE.search(low):
        return ctx.where.chosen
    return None


# ---- one place, ready to show -------------------------------------------------------------------------


def open_text(spot: Spot, ctx: Context) -> str:
    state = open_now(spot.hours, ctx.now)
    return "open now" if state else "closed now" if state is False else ""


def place_item(spot: Spot, ctx: Context, number: int | None = None) -> dict:
    bits = []
    if spot.distance_m is not None:
        bits.append(say_distance(spot.distance_m))
        if spot.distance_m < 2500:
            bits.append(f"{round(max(1, walk_minutes(spot.distance_m)))} min walk")
    state = open_text(spot, ctx)
    if state:
        bits.append(state[0].upper() + state[1:])
    if spot.cuisine:
        bits.append(spot.cuisine)
    if spot.address:
        bits.append(spot.address)
    links = [{"label": "Directions", "url": directions_link(spot.lat, spot.lon, "foot" if (spot.distance_m or 0) < 2500 else "car")}]
    if spot.website:
        links.append({"label": "Website", "url": spot.website})
    if spot.phone:
        links.append({"label": "Call", "url": f"tel:{spot.phone}"})
    return {"title": (f"{number}. " if number else "") + spot.name, "note": " · ".join(bits), "links": links, "lat": spot.lat, "lon": spot.lon}


# ---- the skill ----------------------------------------------------------------------------------------

_FILLER = set(
    "find show me list any some a an the good best great nice cheap top open now places place spots spot near nearby around "
    "here close closest nearest by to is are there where what what's whats can i we get go for of in at on with please chief "
    "hey ok okay so and also my location us area this that one ones something somewhere which do you know recommend suggest "
    "tell look looking search want need like would could should me some walking distance within minutes minute walk from "
    "current any good is there are there right i'm im i am feeling so really very".split()
)
_FORGET = re.compile(r"\b(forget|clear|stop using|delete|remove)\b.*\b(my )?location\b", re.I)
_EAT_SAY = ("places to eat", "places for", "places")


def run(ctx: Context) -> Proposal:
    text = ctx.request.strip()
    if _FORGET.search(text):
        ctx.where.clear()
        return Proposal("nearby", "Done. I've forgotten your location.", [], key=f"nearby:{uuid.uuid4().hex}")

    centre, rest, note, problem = locate(ctx, text)
    if problem:
        return Proposal("nearby", problem, [], key=f"nearby:{uuid.uuid4().hex}")
    words = [w for w in re.findall(r"[a-z0-9']+", rest.lower()) if w not in _FILLER]
    asked = " ".join(words)
    if not asked and note:  # only "I'm at X"
        return Proposal("nearby", f"{note} Ask me what's nearby, or how to get somewhere.", [], key=f"nearby:{uuid.uuid4().hex}")
    if centre is None:
        return ask_for_location(ctx, "nearby")
    if not asked and re.search(r"\bmy location\b", text, re.I):
        return Proposal("nearby", "I have your location. Ask me: sushi near me, or how do I get to the station?", [], key=f"nearby:{uuid.uuid4().hex}")

    want = want_from(asked or "restaurant")
    spots = ctx.tools.find_places(asked or "restaurant", centre.lat, centre.lon, 1500, 8)
    wider = False
    if not spots:
        spots, wider = ctx.tools.find_places(asked or "restaurant", centre.lat, centre.lon, 5000, 8), True
    if re.search(r"\bopen\b", text, re.I):
        spots = [s for s in spots if open_now(s.hours, ctx.now)] or spots
    who = here_label(centre)
    if not spots:
        say = (note + " " if note else "") + f"I couldn't find any {want.say} within 5 km of {who}."
        return Proposal("nearby", say, [], key=f"nearby:{uuid.uuid4().hex}", sources=[CREDIT])

    ctx.where.spots = spots
    first = spots[0]
    shown = spots[:5]
    further = ", a little further out" if wider else ""
    walk = f", about {say_minutes(walk_minutes(first.distance_m or 0))} on foot" if (first.distance_m or 0) < 2500 else ""
    state = open_text(first, ctx)
    about_first = f"{first.name}, {say_distance(first.distance_m or 0)} away{walk}" + (f", {state}" if state else "") + "."
    if len(shown) > 1:
        say = f"Here are the {len(shown)} closest {want.say} near {who}{further}. The closest is {about_first}"
    else:
        say = f"I found one near {who}{further}: {about_first}"
    if len(shown) > 1:
        say += " Then " + " and ".join(f"{s.name} ({say_distance(s.distance_m or 0)})" for s in shown[1:3]) + "."
    eating = want.filters and "amenity" in want.filters[0] and "restaurant" in want.filters[0]
    say += ' Say "directions to the first one"' + (' or "book a table at number 2 for 7pm".' if eating else ".")
    if first.sample:
        say += " These are sample places. Switch to live mode for real ones."
    return Proposal(
        "nearby", (note + " " if note else "") + say, [], key=f"nearby:{uuid.uuid4().hex}",
        sources=[CREDIT], items=[place_item(s, ctx, i) for i, s in enumerate(shown, 1)],
    )


SKILL = Skill(
    label="Nearby",
    name="nearby",
    description="Places around you: restaurants by cuisine (sushi, pizza), cafes, pharmacies, ATMs, with distance and opening hours.",
    triggers=[
        "near me", "nearby", "near here", "around here", "around me", "close to me", "close by", "closest", "nearest",
        "places to eat", "where can i eat", "where should i eat", "where to eat", "somewhere to eat", "something to eat",
        "i'm at", "i am at", "im at", "we're at", "we are at", "my location", "restaurants in", "restaurants near",
        "restaurant near", "cafes near", "coffee near", "food near", "places near", "what's around", "walking distance",
    ],
    allowed_tools={"find_places", "find_location"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

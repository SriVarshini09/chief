"""directions: "how do I get to the first one?", "directions to Golden Gate Park", "from Union Square to the Ferry Building
by bike", "how far is MG Road?".

Read-only. Works out the route from where you are (or from the place you name) on foot, by bike or by car with
OpenStreetMap, says how far and how long, reads the first turns, and gives a Directions link that opens the map
on your phone for turn-by-turn. Buses and trains: the link opens transit directions; Chief can't see live times.
"""

from __future__ import annotations

import re
import uuid

from chief.models import Proposal
from chief.places import CREDIT, Spot, directions_link, meters, say_distance, say_minutes
from chief.skills.base import Context, Skill
from chief.skills.nearby import ask_for_location, here_label, pick

_MODES = [
    ("transit", r"\b(bus|buses|train|trains|metro|subway|tube|transit|public transport|tram)\b"),
    ("bike", r"\b(bike|bicycle|cycle|cycling|biking)\b"),
    ("car", r"\b(drive|driving|car|cab|taxi|uber|ola|auto|rickshaw)\b"),
    ("foot", r"\b(walk|walking|on foot|by foot)\b"),
]
_HOW = {"foot": "on foot", "bike": "by bike", "car": "by car"}
_LEAD = re.compile(
    r"^.*?\b(?:directions?|route|way|navigate|take me|get me|how\s+(?:do|can|should|would)\s+(?:i|we)\s+(?:get|go|reach|walk|drive|cycle)|"
    r"how\s+to\s+(?:get|go|reach)|how\s+far(?:\s+away)?\s+is|how\s+far|how\s+long\s+(?:does\s+it\s+take|to\s+get)|walk|drive|cycle|go)\b"
    r"(?:\s+(?:me|us))?(?:\s+(?:to|towards|till|until|for))?\s*",
    re.I,
)
_TRAIL = re.compile(
    r"\s*(?:\b(?:by|on|in|via|with)\s+(?:a\s+)?(?:car|foot|bike|bicycle|bus|train|metro|subway|transit|public transport|cab|taxi|auto)\b.*|"
    r"\b(?:walking|driving|cycling)\b.*|\bfrom here\b.*|\bplease\b.*|\bright now\b.*|\bnow\b\s*)$",
    re.I,
)


def _mode(text: str) -> str | None:
    for mode, pattern in _MODES:
        if re.search(pattern, text, re.I):
            return mode
    return None


def _split(text: str) -> tuple[str, str]:
    """(origin as said, destination as said). Either may be empty."""
    m = re.search(r"\bfrom\s+(?P<a>.+?)\s+to\s+(?P<b>.+)$", text, re.I)
    if m and m.group("a").strip().lower() not in ("here", "where i am", "my location"):
        return m.group("a").strip(), _TRAIL.sub("", m.group("b")).strip(" ?.!,")
    dest = _LEAD.sub("", text, count=1)
    origin = ""
    m = re.search(r"\s+from\s+(?P<a>.+)$", dest, re.I)
    if m:
        origin, dest = m.group("a"), dest[: m.start()]
        if origin.strip().lower() in ("here", "where i am", "my location"):
            origin = ""
    return _TRAIL.sub("", origin).strip(" ?.!,"), _TRAIL.sub("", dest).strip(" ?.!,")


def _find(ctx: Context, said: str, near) -> Spot | None:
    chosen = pick(ctx, said) if said else None
    if chosen:
        return chosen
    if not said or said.lower() in ("there", "it", "that place"):
        return ctx.where.chosen
    return ctx.tools.find_location(said, near.lat if near else None, near.lon if near else None)


def _id() -> str:
    return f"directions:{uuid.uuid4().hex}"


def run(ctx: Context) -> Proposal:
    text = ctx.request.strip()
    mode_said = _mode(text)
    origin_said, dest_said = _split(text)
    here = ctx.where.here(ctx.now)

    dest = _find(ctx, dest_said, here)
    if dest is None:
        if not dest_said:
            return Proposal("directions", "Where do you want to go? Try: directions to the station, or how do I get to the first one?", [], _id())
        return Proposal("directions", f"I couldn't find {dest_said} on the map. Try the full name or a street.", [], _id())
    ctx.where.chosen = dest

    if origin_said:
        origin = ctx.tools.find_location(origin_said, here.lat if here else None, here.lon if here else None)
        if origin is None:
            return Proposal("directions", f"I couldn't find {origin_said} on the map.", [], _id())
        start, start_name, origin_text = (origin.lat, origin.lon), origin.name, f"{origin.lat:.5f},{origin.lon:.5f}"
    elif here is not None:
        start, start_name, origin_text = (here.lat, here.lon), here_label(here), ""
    else:  # no idea where you are: the map link still works, because the phone knows
        mode = mode_said or "foot"
        link = directions_link(dest.lat, dest.lon, mode)
        return Proposal(
            "directions", f"Here's the way to {dest.name} on the map. " + "Share your location and I'll tell you how far it is and the turns.",
            [], _id(), sources=[CREDIT], links=[{"label": "Directions", "url": link}], needs="location",
        )

    straight = meters(start[0], start[1], dest.lat, dest.lon)
    mode = mode_said or ("foot" if straight <= 2000 else "car")
    where_to = dest.name + (f" ({dest.address})" if dest.address and dest.address.lower() not in dest.name.lower() else "")
    from_text = "" if start_name == "you" else f" from {start_name}"
    links = [{"label": "Directions", "url": directions_link(dest.lat, dest.lon, mode, origin_text)}]

    if mode == "transit":
        say = (f"{where_to} is about {say_distance(straight)}{from_text} as the crow flies. "
               "I can't see live bus or train times, so tap Directions for the transit route and times.")
        return Proposal("directions", say, [], _id(), sources=[CREDIT], links=links)

    try:
        route = ctx.tools.get_route(start[0], start[1], dest.lat, dest.lon, mode)
    except Exception:
        say = (f"I couldn't work out the route just now, but {where_to} is about {say_distance(straight)}{from_text} "
               "as the crow flies. Tap Directions to open it in the map.")
        return Proposal("directions", say, [], _id(), sources=[CREDIT], links=links)

    say = f"{where_to} is {say_distance(route.distance_m)}{from_text}: about {say_minutes(route.minutes)} {_HOW.get(route.mode, '')}."
    turns = [s for s in route.steps if s != "Arrive"][:3]
    if turns:
        say += " " + ", then ".join(t[0].lower() + t[1:] if i else t for i, t in enumerate(turns)) + "."
    say += " Tap Directions to follow it on the map."
    if mode_said is None and route.mode == "foot" and route.minutes > 25:
        links.append({"label": "By car", "url": directions_link(dest.lat, dest.lon, "car", origin_text)})
    if route.mode in ("foot", "car") and straight > 1500:
        links.append({"label": "By bus or train", "url": directions_link(dest.lat, dest.lon, "transit", origin_text)})
    if route.sample:
        say += " (Sample route: switch to live mode for real ones.)"
    items = [{"title": f"{i}. {s}"} for i, s in enumerate(route.steps, 1)]
    return Proposal("directions", say, [], _id(), sources=[CREDIT], links=links, items=items)


SKILL = Skill(
    label="Directions",
    name="directions",
    description="How to get somewhere from where you are, on foot, by bike or by car: distance, time, the turns and a map link.",
    triggers=[
        "directions", "how do i get", "how can i get", "how to get to", "how do i go", "how to go to", "how do i reach",
        "how to reach", "route to", "take me to", "take me there", "navigate to", "walk to", "how far is", "how far to",
        "how far away", "get me to", "the way to", "from here to", "how long to get", "how long does it take to get",
    ],
    allowed_tools={"find_location", "get_route"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

"""Walking guidance: Chief talks you there turn by turn from your live location.

The page (chief/web/guide.js) or Telegram's live location sends where you are every few seconds; `next_cue`
decides what, if anything, to say: the first instruction once, each turn about 45 m before it and again at it,
"Keep going straight" on a long stretch, "You've arrived" within 20 m, and a fresh route when you are walking
away from the next turn. GPS and voice only: no camera.

The decision is pure (a Guide and a position in, a Cue out) and lives here on the server, so Python tests cover
it, the browser and Telegram share one copy, and a new route can be fetched without another round trip.
The guide is kept in memory on Whereabouts, never on disk, like your location.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from chief.places import Spot, Step, Whereabouts, meters

SOON_M = 45.0  # announce a turn this far before it ("In 40 metres, turn left onto Post Street")
AT_M = 15.0  # at the turn: say it again, then look at the next one
ARRIVE_M = 20.0  # this close to the end: you've arrived
WRONG_WAY_M = 30.0  # this much further from the next turn than you have been: walking the wrong way
PASSED_M = 30.0  # been this close to a turn and now walking away from it: you took it (or walked past)
ON_ROAD_M = 25.0  # within this of the stretch after a turn: you took it
REASSURE_M = 250.0  # walked this far since the last thing said, with no turn coming: "Keep going straight"
WORST_FIX_M = 50.0  # ignore positions less accurate than this
REROUTE_EVERY_S = 20.0  # at most one new route this often
MAX_REROUTES = 10
_LOCK = threading.Lock()


@dataclass
class Cue:
    say: str = ""  # what to say now; empty: nothing new
    status: str = "guiding"  # guiding, rerouting, arrived
    reroute: bool = False  # you're walking away from the route: fetch a new one from here
    step: int = 0  # the index of the instruction coming up next
    show: str = ""  # the instruction to show on screen


@dataclass
class Guide:
    name: str  # where to
    lat: float
    lon: float
    steps: list[Step]  # depart ... arrive, each with a place
    mode: str = "foot"
    nxt: int = 0  # the instruction coming up; 0 = nothing said yet
    best_m: float = math.inf  # closest you've been to steps[nxt]
    soon_said: bool = False
    said_at: tuple[float, float] | None = None  # where the last thing was said
    arrived: bool = False
    reroutes: int = 0
    last_reroute: float = -math.inf
    history: list[str] = field(default_factory=list)  # what was said, newest last (the page shows the last)

    @property
    def end(self) -> tuple[float, float]:
        last = self.steps[-1] if self.steps else None
        if last is not None and last.kind == "arrive" and last.lat is not None and last.lon is not None:
            return last.lat, last.lon
        return self.lat, self.lon


def guidable(steps: list[Step]) -> bool:
    return len(steps) >= 2 and all(s.lat is not None and s.lon is not None for s in steps)


def spoken_metres(m: float) -> str:
    if m >= 950:
        km = f"{m / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{km} kilometre" + ("" if km == "1" else "s")
    n = max(10, int(round(m / 10.0)) * 10)
    return f"{n} metres"


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _what(g: Guide, step: Step) -> str:
    """What to do at a step: 'turn left onto Post Street', or for the end, 'Sakura Sushi Bar is on your right'."""
    if step.kind == "arrive":
        side = step.say.split("on your ", 1)[1] if "on your " in step.say else ""
        return f"{g.name} is on your {side}" if side else f"you'll reach {g.name}"
    return _lower(step.say)


def _to_segment(lat: float, lon: float, a: Step, b: Step) -> float:
    """Metres from a point to the straight stretch a-b (flat earth: fine over a few hundred metres)."""
    k = math.cos(math.radians(lat)) * 111_320.0
    ax, ay, bx, by = (a.lon - lon) * k, (a.lat - lat) * 110_540.0, (b.lon - lon) * k, (b.lat - lat) * 110_540.0
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def _said(g: Guide, cue: Cue, lat: float, lon: float) -> Cue:
    if cue.say:
        g.said_at = (lat, lon)
        g.history = (g.history + [cue.say])[-20:]
    cue.step = g.nxt
    if not cue.show:
        cue.show = g.history[-1] if g.history else ""
    return cue


def _advance(g: Guide) -> None:
    g.nxt += 1
    g.best_m, g.soon_said = math.inf, False


def next_cue(g: Guide, lat: float, lon: float, accuracy: float | None = None) -> Cue:
    """What to say at this position, if anything. Updates the guide. Pure: no network, no clock."""
    if g.arrived:
        return _said(g, Cue("", "arrived"), lat, lon)
    if accuracy is not None and accuracy > WORST_FIX_M:
        return _said(g, Cue(""), lat, lon)  # a poor fix: wait for a better one
    end_lat, end_lon = g.end
    if meters(lat, lon, end_lat, end_lon) <= ARRIVE_M:
        g.arrived = True
        return _said(g, Cue(f"You've arrived at {g.name}.", "arrived"), lat, lon)

    if g.nxt == 0:  # the first instruction, once
        first = g.steps[0]
        _advance(g)
        say = first.say + (f" for {spoken_metres(first.distance_m)}" if first.distance_m >= 5 else "") + "."
        return _said(g, Cue(say), lat, lon)

    last = len(g.steps) - 1
    g.nxt = min(g.nxt, last)
    step = g.steps[g.nxt]
    d = meters(lat, lon, step.lat, step.lon)

    if step.kind != "arrive":
        after = g.steps[g.nxt + 1]
        if d <= AT_M:  # at the turn
            _advance(g)
            say = step.say + "."
            rest = max(step.distance_m, meters(step.lat, step.lon, after.lat, after.lon))
            if rest > SOON_M * 2:
                say += f" Then keep going for {spoken_metres(rest)}."
            return _said(g, Cue(say), lat, lon)
        if g.best_m <= PASSED_M and d > g.best_m + 10 and _to_segment(lat, lon, step, after) <= ON_ROAD_M:
            _advance(g)  # you took the turn without passing close to its exact point (GPS wander): carry on quietly
            return next_cue(g, lat, lon, accuracy)

    if d > g.best_m + WRONG_WAY_M:  # walking away from where you need to be next
        g.best_m = d  # ask again only after another 30 m the wrong way
        return _said(g, Cue("This isn't the way. Finding a new route.", "rerouting", reroute=True), lat, lon)
    g.best_m = min(g.best_m, d)

    if not g.soon_said and d <= SOON_M:
        g.soon_said = True
        return _said(g, Cue(f"In {spoken_metres(d)}, {_what(g, step)}."), lat, lon)

    if g.said_at and meters(lat, lon, *g.said_at) >= REASSURE_M and d > SOON_M * 2:
        street = g.steps[g.nxt - 1].street if g.nxt >= 1 else ""
        nxt = f"In {spoken_metres(d)}, {_what(g, step)}."
        return _said(g, Cue(f"Keep going straight{' on ' + street if street else ''}. {nxt}"), lat, lon)
    return _said(g, Cue(""), lat, lon)


# ---- the session around it: start, a new position, stop ------------------------------------------------


def _destination(where: Whereabouts, to: dict | None) -> Spot | None:
    chosen = where.chosen
    if to and isinstance(to.get("lat"), (int, float)) and isinstance(to.get("lon"), (int, float)):
        lat, lon = float(to["lat"]), float(to["lon"])
        if chosen and meters(lat, lon, chosen.lat, chosen.lon) <= 150:
            return chosen
        return Spot(str(to.get("name") or "your destination")[:80], lat, lon)
    return chosen


def _route(tools, lat: float, lon: float, dest: Spot) -> list[Step]:
    route = tools.get_route(lat, lon, dest.lat, dest.lon, "foot")
    steps = list(getattr(route, "points", []) or [])
    if not guidable(steps):
        raise LookupError("this route has no turn locations")
    return steps


def start(where: Whereabouts, tools, lat: float, lon: float, to: dict | None = None, accuracy: float | None = None) -> dict:
    """Start guiding from here to `to` ({"lat", "lon", "name"}) or the place last talked about."""
    dest = _destination(where, to)
    if dest is None:
        return {"active": False, "say": "Where to? Ask for directions first, like: directions to the first one."}
    try:
        steps = _route(tools, lat, lon, dest)
    except Exception:
        return {"active": False, "say": f"I couldn't work out a walking route to {dest.name} just now. Tap Directions for the map."}
    g = Guide(dest.name, dest.lat, dest.lon, steps)
    cue = next_cue(g, lat, lon, None)
    with _LOCK:
        where.guide = None if g.arrived else g
    if g.arrived:
        return {"active": False, "status": "arrived", "say": cue.say, "show": cue.show, "to": dest.name}
    say = f"Okay, walking you to {dest.name}. {cue.say}"
    return {"active": True, "status": cue.status, "say": say, "show": cue.show, "to": dest.name}


def tick(where: Whereabouts, tools, lat: float, lon: float, accuracy: float | None = None,
         clock: Callable[[], float] = time.monotonic) -> dict:
    """A new position while guiding: what to say, and a new route when you've gone the wrong way."""
    with _LOCK:  # the page and Telegram may both send positions
        return _tick(where, tools, lat, lon, accuracy, clock)


def _tick(where: Whereabouts, tools, lat: float, lon: float, accuracy: float | None, clock: Callable[[], float]) -> dict:
    g = where.guide
    if g is None:
        return {"active": False, "status": "off", "say": "", "show": ""}
    cue = next_cue(g, lat, lon, accuracy)
    if cue.reroute:
        now = clock()
        if g.reroutes >= MAX_REROUTES or now - g.last_reroute < REROUTE_EVERY_S:
            cue = Cue("", "guiding", show=cue.show)
        else:
            g.reroutes, g.last_reroute = g.reroutes + 1, now
            try:
                steps = _route(tools, lat, lon, Spot(g.name, g.lat, g.lon))
            except Exception:
                cue.say = "This isn't the way, and I couldn't find a new route. Tap Directions for the map."
            else:
                fresh = Guide(g.name, g.lat, g.lon, steps, reroutes=g.reroutes, last_reroute=g.last_reroute, history=g.history)
                where.guide = g = fresh
                first = next_cue(g, lat, lon, None)
                cue = Cue(f"This isn't the way, so here's a new route. {first.say}", first.status, show=first.show)
    if g.arrived:
        where.guide = None  # done: nothing more to keep
    return {"active": not g.arrived, "status": cue.status, "say": cue.say, "show": cue.show or cue.say}


def stop(where: Whereabouts) -> dict:
    with _LOCK:
        was = where.guide is not None
        where.guide = None
    return {"active": False, "status": "off", "say": "Okay, I've stopped guiding." if was else ""}

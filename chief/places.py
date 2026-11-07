"""Where you are, what's around you, and how to get there.

Free OpenStreetMap services, no key needed:
  Overpass   places by type and cuisine (the best data, but often busy: a short timeout, then a 10-minute pause)
  Nominatim  places and addresses by name (at most one request a second, as its usage policy asks)
  Photon     a second name search when Nominatim finds nothing
  OSRM       walking, cycling and driving routes (routing.openstreetmap.de runs one server per way of travel)

Privacy: your location is kept in memory only. It is never written to disk, the demo cache or the activity
log as more than a rounded point. Coordinates sent for a search are rounded to about 100 m, for a route to about
10 m. CHIEF_PLACES=off stops every lookup. Map data (c) OpenStreetMap contributors, ODbL.

Chief never books or pays. It finds the place, gets the details ready and opens the booking page: you finish it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

USER_AGENT = "chief-agent/0.1 (open-source personal assistant; https://github.com/SriVarshini09/chief)"
CREDIT = "Map data (c) OpenStreetMap contributors"
OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
PHOTON = "https://photon.komoot.io/api/"
ROUTERS = {"foot": "routed-foot", "bike": "routed-bike", "car": "routed-car"}
ROUTER = "https://routing.openstreetmap.de/{profile}/route/v1/driving/{a};{b}?overview=false&steps=true"
WALK_M_PER_MIN = 80.0  # about 4.8 km an hour


class PlacesOff(RuntimeError):
    """CHIEF_PLACES=off: no lookups at all."""


def enabled() -> bool:
    return os.environ.get("CHIEF_PLACES", "on").strip().lower() not in ("off", "0", "no", "false")


# ---- what we know about a place -----------------------------------------------------------------------


@dataclass
class Spot:
    name: str
    lat: float
    lon: float
    kind: str = ""  # restaurant, cafe, pharmacy ...
    cuisine: str = ""  # "sushi, japanese"
    address: str = ""
    hours: str = ""  # OpenStreetMap opening_hours, e.g. "Mo-Fr 11:00-22:00; Sa,Su 12:00-23:00"
    website: str = ""
    phone: str = ""
    distance_m: float | None = None
    sample: bool = False  # made-up data from SamplePlaces


@dataclass
class Here:
    lat: float
    lon: float
    label: str = ""
    source: str = ""  # "browser", "telegram", "said"
    at: datetime | None = None


@dataclass
class Step:
    """One instruction of a route and where it happens, for walking guidance. Memory only, like the route."""
    text: str  # as read out: "Turn left onto Hyde Street for 1.2 km"
    say: str  # the instruction alone: "Turn left onto Hyde Street"
    lat: float | None = None  # where the turn is (OSRM maneuver.location); None if the router did not say
    lon: float | None = None
    distance_m: float = 0.0  # from this point to the next instruction
    kind: str = ""  # depart, turn, roundabout, arrive ...
    street: str = ""


@dataclass
class Route:
    mode: str  # foot, bike, car
    distance_m: float
    minutes: float
    steps: list[str] = field(default_factory=list)
    sample: bool = False
    points: list[Step] = field(default_factory=list)  # every instruction with its place, depart to arrive (not cut at 14)


class Whereabouts:
    """Your last known location and the last places Chief listed ("the first one"). Memory only."""

    FRESH = timedelta(hours=6)

    def __init__(self) -> None:
        self._here: Here | None = None
        self.spots: list[Spot] = []  # the last list Chief read out: "the first one", "number 2"
        self.chosen: Spot | None = None  # the place we were just talking about: "take me there"
        self.guide: Any = None  # a walk being guided turn by turn (chief.guide.Guide), memory only
        self.lock = threading.Lock()

    def set(self, lat: float, lon: float, label: str = "", source: str = "", now: datetime | None = None) -> Here:
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0.0, 0.0):
            raise ValueError("that is not a place on Earth")
        with self.lock:
            self._here = Here(lat, lon, label.strip()[:120], source, now or datetime.now())
            return self._here

    def here(self, now: datetime | None = None) -> Here | None:
        with self.lock:
            h = self._here
        if h is None:
            return None
        if h.at and now and now - h.at > self.FRESH:
            return None  # a location from this morning is not where you are now
        return h

    def clear(self) -> None:
        with self.lock:
            self._here, self.spots, self.chosen, self.guide = None, [], None, None


# ---- distances, times and links -----------------------------------------------------------------------


def meters(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def say_distance(m: float) -> str:
    if m < 950:
        return f"{max(10, round(m / 10) * 10):.0f} m"
    return f"{m / 1000:.1f} km".replace(".0 km", " km")


def say_minutes(minutes: float) -> str:
    minutes = max(1, round(minutes))
    if minutes < 60:
        return f"{minutes} minute{'s' * (minutes != 1)}"
    h, m = divmod(minutes, 60)
    return f"{h} hour{'s' * (h != 1)}" + (f" {m} minute{'s' * (m != 1)}" if m else "")


def walk_minutes(m: float) -> float:
    return m / WALK_M_PER_MIN


_TRAVEL = {"foot": "walking", "bike": "bicycling", "car": "driving", "transit": "transit"}


def directions_link(lat: float, lon: float, mode: str = "foot", origin: str = "") -> str:
    """Google Maps directions. With no origin the phone starts from where it is, which is the most exact."""
    q = {"api": "1", "destination": f"{lat:.6f},{lon:.6f}", "travelmode": _TRAVEL.get(mode, "walking")}
    if origin:
        q["origin"] = origin
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(q)


def map_link(spot: Spot) -> str:
    return f"https://www.openstreetmap.org/?mlat={spot.lat:.6f}&mlon={spot.lon:.6f}#map=18/{spot.lat:.6f}/{spot.lon:.6f}"


def search_link(text: str) -> str:
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": text})


def safe_url(url: str) -> str:
    """Only plain web links from map data: never javascript:, data: or anything else."""
    url = (url or "").strip().split(";")[0].strip()
    if url and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or any(c in url for c in ' "<>\\'):
        return ""
    return url


def safe_phone(phone: str) -> str:
    first = (phone or "").split(";")[0]
    digits = re.sub(r"[^\d+]", "", first)
    return digits if 6 <= len(digits.lstrip("+")) <= 16 and "+" not in digits[1:] else ""


# ---- "is it open now?" from OpenStreetMap opening_hours -------------------------------------------------

_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_RULE = re.compile(r"^\s*(?P<days>(?:(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:\s*-\s*(?:Mo|Tu|We|Th|Fr|Sa|Su))?\s*,?\s*)+)?\s*(?P<times>.*?)\s*$")
_SPAN = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\+?$")


def _days(spec: str) -> set[int] | None:
    out: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        a, _, b = part.partition("-")
        a, b = a.strip(), b.strip()
        if a not in _DAYS or (b and b not in _DAYS):
            return None
        i, j = _DAYS.index(a), _DAYS.index(b or a)
        out.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
    return out


def _spans(spec: str) -> list[tuple[int, int]] | None:
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        m = _SPAN.match(part)
        if not m:
            return None
        h1, m1, h2, m2 = map(int, m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:
            end += 24 * 60  # past midnight
        out.append((start, end))
    return out


def open_now(hours: str, now: datetime) -> bool | None:
    """True or False when the hours say so plainly; None when they are missing or too fancy to read."""
    hours = (hours or "").strip()
    if not hours:
        return None
    if hours == "24/7":
        return True
    rules: list[tuple[set[int], list[tuple[int, int]]]] = []
    for raw in hours.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        m = _RULE.match(raw)
        if not m:
            return None
        days = _days(m.group("days")) if m.group("days") else set(range(7))
        times = m.group("times").strip().lower()
        if days is None:
            return None
        if times in ("off", "closed"):
            spans: list[tuple[int, int]] | None = []
        elif times == "24/7" or times == "00:00-24:00":
            spans = [(0, 24 * 60)]
        else:
            spans = _spans(m.group("times"))
        if spans is None:
            return None
        rules.append((days, spans))
    if not rules:
        return None

    def spans_for(day: int) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        for days, spans in rules:  # a later rule for the same day replaces an earlier one
            if day in days:
                found = spans
        return found

    today, minute = now.weekday(), now.hour * 60 + now.minute
    if any(a <= minute < b for a, b in spans_for(today)):
        return True
    yesterday = (today - 1) % 7
    return any(a <= minute + 24 * 60 < b for a, b in spans_for(yesterday) if b > 24 * 60)


# ---- reading "sushi", "coffee", "a pharmacy" ------------------------------------------------------------

_EAT = '["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]'
CUISINES: dict[str, tuple[str, str]] = {  # word -> (cuisine pattern, how to say it)
    "sushi": ("sushi|japanese", "sushi places"), "japanese": ("japanese|sushi|ramen", "Japanese places"),
    "ramen": ("ramen|japanese|noodle", "ramen places"), "pizza": ("pizza", "pizza places"),
    "burger": ("burger", "burger places"), "burgers": ("burger", "burger places"), "indian": ("indian", "Indian places"),
    "south indian": ("south_indian|indian", "South Indian places"), "dosa": ("south_indian|indian", "dosa places"),
    "biryani": ("biryani|indian", "biryani places"), "chinese": ("chinese", "Chinese places"), "thai": ("thai", "Thai places"),
    "italian": ("italian|pizza", "Italian places"), "mexican": ("mexican|tex-mex|tacos", "Mexican places"),
    "tacos": ("mexican|tacos", "taco places"), "korean": ("korean", "Korean places"), "vietnamese": ("vietnamese", "Vietnamese places"),
    "pho": ("vietnamese", "pho places"), "vegan": ("vegan", "vegan places"), "vegetarian": ("vegetarian|vegan", "vegetarian places"),
    "seafood": ("seafood|fish", "seafood places"), "bbq": ("bbq|barbecue", "barbecue places"), "barbecue": ("bbq|barbecue", "barbecue places"),
    "steak": ("steak", "steakhouses"), "kebab": ("kebab", "kebab places"), "noodles": ("noodle|chinese|asian", "noodle places"),
    "dim sum": ("dim_sum|chinese", "dim sum places"), "mediterranean": ("mediterranean|greek|lebanese", "Mediterranean places"),
    "greek": ("greek", "Greek places"), "french": ("french", "French places"), "american": ("american|burger", "American places"),
    "boba": ("bubble_tea", "bubble tea places"), "bubble tea": ("bubble_tea", "bubble tea places"),
    "dessert": ("dessert|ice_cream|cake", "dessert places"), "breakfast": ("breakfast|brunch", "breakfast places"),
    "brunch": ("brunch|breakfast", "brunch places"), "chicken": ("chicken", "chicken places"), "sandwich": ("sandwich", "sandwich places"),
}
KINDS: dict[str, tuple[list[str], str, str]] = {  # word -> (Overpass filters, name search, how to say it)
    "restaurant": ([_EAT], "restaurant", "places to eat"), "restaurants": ([_EAT], "restaurant", "places to eat"),
    "food": ([_EAT], "restaurant", "places to eat"), "eat": ([_EAT], "restaurant", "places to eat"),
    "dinner": ([_EAT], "restaurant", "places for dinner"), "lunch": ([_EAT], "restaurant", "places for lunch"),
    "cafe": (['["amenity"="cafe"]'], "cafe", "cafes"), "cafes": (['["amenity"="cafe"]'], "cafe", "cafes"),
    "coffee": (['["amenity"="cafe"]'], "cafe", "cafes"), "bar": (['["amenity"~"^(bar|pub)$"]'], "bar", "bars"),
    "bars": (['["amenity"~"^(bar|pub)$"]'], "bar", "bars"), "pub": (['["amenity"~"^(bar|pub)$"]'], "pub", "pubs"),
    "drinks": (['["amenity"~"^(bar|pub)$"]'], "bar", "bars"), "bakery": (['["shop"="bakery"]'], "bakery", "bakeries"),
    "ice cream": (['["amenity"="ice_cream"]', '["shop"="ice_cream"]'], "ice cream", "ice cream places"),
    "pharmacy": (['["amenity"="pharmacy"]'], "pharmacy", "pharmacies"), "chemist": (['["amenity"="pharmacy"]'], "pharmacy", "pharmacies"),
    "medical store": (['["amenity"="pharmacy"]'], "pharmacy", "pharmacies"), "hospital": (['["amenity"="hospital"]'], "hospital", "hospitals"),
    "clinic": (['["amenity"~"^(clinic|doctors)$"]'], "clinic", "clinics"), "doctor": (['["amenity"~"^(clinic|doctors)$"]'], "doctor", "doctors"),
    "atm": (['["amenity"="atm"]'], "atm", "ATMs"), "bank": (['["amenity"="bank"]'], "bank", "banks"),
    "petrol": (['["amenity"="fuel"]'], "fuel", "fuel stations"), "gas station": (['["amenity"="fuel"]'], "fuel", "fuel stations"),
    "fuel": (['["amenity"="fuel"]'], "fuel", "fuel stations"), "parking": (['["amenity"="parking"]'], "parking", "car parks"),
    "ev charging": (['["amenity"="charging_station"]'], "charging station", "charging stations"),
    "hotel": (['["tourism"~"^(hotel|guest_house|hostel|motel)$"]'], "hotel", "hotels"),
    "hotels": (['["tourism"~"^(hotel|guest_house|hostel|motel)$"]'], "hotel", "hotels"),
    "supermarket": (['["shop"~"^(supermarket|convenience)$"]'], "supermarket", "supermarkets"),
    "grocery": (['["shop"~"^(supermarket|convenience|greengrocer)$"]'], "supermarket", "grocery shops"),
    "park": (['["leisure"="park"]'], "park", "parks"), "parks": (['["leisure"="park"]'], "park", "parks"),
    "museum": (['["tourism"="museum"]'], "museum", "museums"), "cinema": (['["amenity"="cinema"]'], "cinema", "cinemas"),
    "movie": (['["amenity"="cinema"]'], "cinema", "cinemas"), "movies": (['["amenity"="cinema"]'], "cinema", "cinemas"),
    "theatre": (['["amenity"~"^(theatre|cinema)$"]'], "theatre", "theatres"), "gym": (['["leisure"="fitness_centre"]'], "gym", "gyms"),
    "toilet": (['["amenity"="toilets"]'], "toilets", "public toilets"), "restroom": (['["amenity"="toilets"]'], "toilets", "public toilets"),
    "washroom": (['["amenity"="toilets"]'], "toilets", "public toilets"),
    "metro": (['["railway"="station"]', '["station"="subway"]'], "metro station", "stations"),
    "train station": (['["railway"="station"]'], "train station", "train stations"),
    "station": (['["railway"="station"]'], "station", "stations"), "bus stop": (['["highway"="bus_stop"]'], "bus stop", "bus stops"),
}


@dataclass
class Want:
    """What to look for."""

    say: str  # "sushi places"
    filters: list[str]  # Overpass tag filters; empty = search by name only
    query: str  # what to type into a name search


def want_from(text: str) -> Want:
    t = " " + re.sub(r"[^a-z0-9' ]+", " ", text.lower()) + " "
    for word in sorted(CUISINES, key=len, reverse=True):
        if f" {word} " in t or f" {word}s " in t:
            pattern, say = CUISINES[word]
            return Want(say, [f'{_EAT}["cuisine"~"{pattern}",i]'], word)
    for word in sorted(KINDS, key=len, reverse=True):
        if f" {word} " in t:
            filters, query, say = KINDS[word]
            return Want(say, list(filters), query)
    name = re.sub(r"\s+", " ", t).strip()
    return Want(f'places called "{name}"' if name else "places to eat", [] if name else [_EAT], name or "restaurant")


# ---- the real thing: OpenStreetMap --------------------------------------------------------------------

Fetch = Callable[[str, str, dict], Any]  # (method, url, params) -> parsed JSON


def _http(method: str, url: str, params: dict, timeout: float) -> Any:
    data = None
    if method == "POST":
        data = urllib.parse.urlencode(params).encode()
    elif params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https hosts above
        return json.loads(r.read(4_000_000))


def _round(v: float, places: int) -> float:
    return round(float(v), places)


def _address(tags: dict) -> str:
    street = " ".join(x for x in (tags.get("addr:housenumber", ""), tags.get("addr:street", "")) if x)
    return ", ".join(x for x in (street, tags.get("addr:suburb", "") or tags.get("addr:city", "")) if x)[:120]


def _spot_from_tags(name: str, lat: float, lon: float, tags: dict, kind: str = "") -> Spot:
    return Spot(
        name=name[:100], lat=float(lat), lon=float(lon), kind=kind or tags.get("amenity") or tags.get("shop") or tags.get("tourism") or "",
        cuisine=", ".join(c.replace("_", " ") for c in str(tags.get("cuisine", "")).split(";") if c)[:80],
        address=_address(tags), hours=str(tags.get("opening_hours", ""))[:200],
        website=safe_url(tags.get("website") or tags.get("contact:website") or ""),
        phone=safe_phone(tags.get("phone") or tags.get("contact:phone") or ""),
    )


class OpenMap:
    def __init__(self, timeout: float = 8.0, fetch: Fetch | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.timeout = timeout
        self.fetch = fetch or (lambda method, url, params: _http(method, url, params, timeout))
        self.sleep, self.clock = sleep, clock
        self._overpass_down_until = 0.0
        self._last_nominatim = -10.0
        self._lock = threading.Lock()

    def _check(self) -> None:
        if not enabled():
            raise PlacesOff("place lookups are turned off (CHIEF_PLACES=off)")

    def _nominatim(self, params: dict) -> list:
        with self._lock:  # their policy: no more than one request a second
            wait = 1.0 - (self.clock() - self._last_nominatim)
            if wait > 0:
                self.sleep(wait)
            self._last_nominatim = self.clock()
        out = self.fetch("GET", NOMINATIM, {"format": "jsonv2", **params})
        return out if isinstance(out, list) else []

    # -- places
    def find_places(self, what: str, lat: float, lon: float, radius_m: int = 1500, limit: int = 8) -> list[Spot]:
        self._check()
        want = want_from(what)
        lat, lon = _round(lat, 3), _round(lon, 3)  # about 100 m: enough to search, not enough to pin you
        radius_m = int(min(max(radius_m, 200), 10000))
        spots: list[Spot] = []
        if want.filters and self.clock() >= self._overpass_down_until:
            try:
                spots = self._overpass(want, lat, lon, radius_m)
            except Exception:
                self._overpass_down_until = self.clock() + 600  # busy: use the name searches for ten minutes
        if not spots:
            spots = self._nominatim_places(want, lat, lon, radius_m)
        if not spots:
            spots = self._photon_places(want, lat, lon)
        return _closest(spots, lat, lon, radius_m * 1.5, limit)

    def _overpass(self, want: Want, lat: float, lon: float, radius_m: int) -> list[Spot]:
        parts = "".join(f"nwr{f}[\"name\"](around:{radius_m},{lat},{lon});" for f in want.filters)
        query = f"[out:json][timeout:{int(self.timeout)}];({parts});out center tags 80;"
        data = self.fetch("POST", OVERPASS, {"data": query}) or {}
        out = []
        for e in data.get("elements", []):
            tags = e.get("tags") or {}
            p_lat, p_lon = e.get("lat", (e.get("center") or {}).get("lat")), e.get("lon", (e.get("center") or {}).get("lon"))
            if tags.get("name") and p_lat is not None and p_lon is not None:
                out.append(_spot_from_tags(tags["name"], p_lat, p_lon, tags))
        return out

    def _nominatim_places(self, want: Want, lat: float, lon: float, radius_m: int) -> list[Spot]:
        dlat = radius_m / 111_000
        dlon = dlat / max(0.2, math.cos(math.radians(lat)))
        rows = self._nominatim({
            "q": want.query, "limit": 25, "bounded": 1, "extratags": 1, "addressdetails": 1,
            "viewbox": f"{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f},{lat - dlat:.4f}",
        })
        out = []
        for r in rows:
            name = r.get("name") or ""
            if not name or r.get("lat") is None:
                continue
            tags = dict(r.get("extratags") or {})
            a = r.get("address") or {}
            tags.setdefault("addr:street", a.get("road", ""))
            tags.setdefault("addr:housenumber", a.get("house_number", ""))
            tags.setdefault("addr:suburb", a.get("suburb", "") or a.get("neighbourhood", ""))
            out.append(_spot_from_tags(name, float(r["lat"]), float(r["lon"]), tags, kind=r.get("type", "")))
        return out

    def _photon_places(self, want: Want, lat: float, lon: float) -> list[Spot]:
        data = self.fetch("GET", PHOTON, {"q": want.query, "lat": lat, "lon": lon, "limit": 15}) or {}
        out = []
        for f in data.get("features", []):
            p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
            if p.get("name") and len(g) == 2:
                tags = {"addr:street": p.get("street", ""), "addr:housenumber": p.get("housenumber", ""), "addr:city": p.get("city", "")}
                out.append(_spot_from_tags(p["name"], g[1], g[0], tags, kind=p.get("osm_value", "")))
        return out

    # -- one place by name or address
    def find_location(self, text: str, near_lat: float | None = None, near_lon: float | None = None) -> Spot | None:
        self._check()
        text = text.strip()[:200]
        if not text:
            return None
        params: dict = {"q": text, "limit": 1, "addressdetails": 1, "extratags": 1}
        if near_lat is not None and near_lon is not None:  # prefer matches near you, but don't insist
            la, lo = _round(near_lat, 2), _round(near_lon, 2)
            params["viewbox"] = f"{lo - .25:.2f},{la + .25:.2f},{lo + .25:.2f},{la - .25:.2f}"
        rows = self._nominatim(params)
        if rows:
            r = rows[0]
            label = r.get("name") or r.get("display_name", "").split(",")[0]
            a = r.get("address") or {}
            town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""
            if town:  # "Union Square, San Francisco", not "Union Square, Community Board 5, ..."
                area = town if town.lower() != (label or "").lower() else a.get("state", "")
            else:
                area = ", ".join(x for x in (r.get("display_name") or "").split(", ")[1:3] if x)
            spot = _spot_from_tags(label or text, float(r["lat"]), float(r["lon"]), r.get("extratags") or {}, kind=r.get("type", ""))
            spot.address = spot.address or area
            return spot
        q: dict = {"q": text, "limit": 1}
        if near_lat is not None and near_lon is not None:
            q.update(lat=_round(near_lat, 2), lon=_round(near_lon, 2))
        data = self.fetch("GET", PHOTON, q) or {}
        for f in data.get("features", []):
            p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
            if len(g) == 2:
                return Spot(p.get("name") or text, g[1], g[0], kind=p.get("osm_value", ""), address=", ".join(x for x in (p.get("city"), p.get("country")) if x))
        return None

    # -- a route
    def get_route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float, mode: str = "foot") -> Route:
        self._check()
        mode = mode if mode in ROUTERS else "foot"
        a = f"{_round(from_lon, 4)},{_round(from_lat, 4)}"
        b = f"{_round(to_lon, 6)},{_round(to_lat, 6)}"
        data = self.fetch("GET", ROUTER.format(profile=ROUTERS[mode], a=a, b=b), {}) or {}
        routes = data.get("routes") or []
        if data.get("code") != "Ok" or not routes:
            raise LookupError(f"no {mode} route found ({data.get('code') or 'no answer'})")
        r = routes[0]
        steps = [s for leg in r.get("legs", []) for s in leg.get("steps", [])]
        return Route(mode, float(r.get("distance", 0)), float(r.get("duration", 0)) / 60, _steps(steps), points=_points(steps))


def _closest(spots: list[Spot], lat: float, lon: float, within: float, limit: int) -> list[Spot]:
    seen, out = set(), []
    for s in spots:
        s.distance_m = meters(lat, lon, s.lat, s.lon)
    for s in sorted(spots, key=lambda s: s.distance_m or 0):
        key = s.name.lower()
        if key in seen or (s.distance_m or 0) > within:
            continue
        seen.add(key)
        out.append(s)
    return out[:limit]


_COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def _instruction(s: dict, street: str) -> tuple[str, str] | None:
    """(kind, what to do) for one OSRM step, or None when it says nothing new (still the same road)."""
    m = s.get("maneuver") or {}
    kind, mod, name = m.get("type", ""), (m.get("modifier") or "").replace("_", " "), s.get("name") or ""
    onto = f" onto {name}" if name else ""
    if kind == "depart":
        heading = _COMPASS[int(((m.get("bearing_after") or 0) + 22.5) // 45) % 8]
        return kind, f"Head {heading}" + (f" on {name}" if name else "")
    if kind == "arrive":
        return kind, "Arrive" + (" on your " + mod if mod in ("left", "right") else "")
    if kind in ("roundabout", "rotary", "roundabout turn"):
        return kind, f"At the roundabout, take exit {m.get('exit') or 1}{onto}"
    if kind in ("new name", "continue") or mod == "straight":
        if not name or name == street:
            return None  # still the same road: nothing new to say
        return kind, f"Continue{onto}"
    if kind == "end of road":
        return kind, f"At the end of the road, turn {mod}{onto}"
    if kind in ("fork", "merge", "on ramp", "off ramp"):
        return kind, f"Keep {mod or 'ahead'}{onto}"
    return kind or "turn", f"Turn {mod}{onto}" if mod else f"Go{onto}"


def _steps(raw: list[dict]) -> list[str]:
    out: list[str] = []
    street = ""  # the road the last instruction put you on
    for s in raw:
        said = _instruction(s, street)
        if said is None:
            continue
        kind, text = said
        if kind == "arrive":
            out.append(text)
            break
        dist = float(s.get("distance") or 0)
        if dist >= 5:
            text += f" for {say_distance(dist)}"
        street = s.get("name") or street
        out.append(text)
        if len(out) >= 14:
            out.append("Then follow the map the rest of the way")
            break
    return out


def _points(raw: list[dict]) -> list[Step]:
    """Every instruction with where it happens (maneuver.location is [lon, lat]) and how far the next stretch is.
    A step that says nothing new adds its distance to the one before. Not cut short: guidance needs them all."""
    out: list[Step] = []
    street = ""
    for s in raw:
        dist = float(s.get("distance") or 0)
        said = _instruction(s, street)
        if said is None:
            if out:
                out[-1].distance_m += dist
            continue
        kind, say = said
        loc = (s.get("maneuver") or {}).get("location") or []
        lat, lon = (float(loc[1]), float(loc[0])) if len(loc) == 2 else (None, None)
        name = s.get("name") or ""
        arrive = kind == "arrive"
        text = say + (f" for {say_distance(dist)}" if dist >= 5 and not arrive else "")
        out.append(Step(text, say, lat, lon, 0.0 if arrive else dist, kind, name))
        if arrive:
            break
        street = name or street
    return out


# ---- sample places, for mock mode and the tests --------------------------------------------------------

SAMPLE_HOME = (37.7880, -122.4075, "Union Square, San Francisco")
_SAMPLE_NAMES = {
    "sushi": ["Sakura Sushi Bar", "Koi Sushi & Ramen", "Hana Japanese Kitchen", "Umi Sushi"],
    "cafe": ["Morning Leaf Cafe", "The Copper Kettle", "Little Bean"],
    "pharmacy": ["Corner Pharmacy", "Union Drug Store"],
}
_SAMPLE_OFFSETS = [(0.0021, 0.0012), (-0.0034, 0.0029), (0.0047, -0.0041), (-0.0062, -0.0055)]
_SAMPLE_HOURS = ["Mo-Su 11:00-22:00", "Mo-Sa 12:00-15:00,17:30-22:30; Su off", "Tu-Su 17:00-23:00", ""]


class SamplePlaces:
    """Made-up places around wherever you are, so mock mode and the tests work offline. Always marked sample."""

    def find_places(self, what: str, lat: float, lon: float, radius_m: int = 1500, limit: int = 8) -> list[Spot]:
        want = want_from(what)
        key = "sushi" if "sushi" in want.say or "Japanese" in want.say else "cafe" if "cafe" in want.say else "pharmacy" if "pharmac" in want.say else ""
        names = _SAMPLE_NAMES.get(key) or ["Green Leaf Kitchen", "Harbor Grill", "Corner Bistro"]
        spots = []
        for i, name in enumerate(names):
            dlat, dlon = _SAMPLE_OFFSETS[i % len(_SAMPLE_OFFSETS)]
            spots.append(Spot(
                name, lat + dlat, lon + dlon, kind="restaurant", cuisine=key or "", address=f"{120 + 40 * i} Sample Street",
                hours=_SAMPLE_HOURS[i % len(_SAMPLE_HOURS)], website=f"https://example.com/{re.sub(r'[^a-z]+', '-', name.lower()).strip('-')}",
                phone=f"+1555010{i:02d}", sample=True,
            ))
        return _closest(spots, lat, lon, radius_m * 1.5, limit)

    def find_location(self, text: str, near_lat: float | None = None, near_lon: float | None = None) -> Spot | None:
        text = text.strip()
        if not text:
            return None
        if "union square" in text.lower():
            return Spot(SAMPLE_HOME[2], SAMPLE_HOME[0], SAMPLE_HOME[1], kind="square", sample=True)
        base_lat, base_lon = (near_lat, near_lon) if near_lat is not None and near_lon is not None else SAMPLE_HOME[:2]
        h = hashlib.sha256(text.lower().encode()).digest()
        return Spot(text.title(), base_lat + (h[0] - 128) / 9000, base_lon + (h[1] - 128) / 9000, kind="place", sample=True)

    def get_route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float, mode: str = "foot") -> Route:
        """An L-shaped walk: along Powell Street to the destination's latitude, then along Post Street to it."""
        d = meters(from_lat, from_lon, to_lat, to_lon) * 1.3
        speed = {"foot": WALK_M_PER_MIN, "bike": 250.0, "car": 450.0}.get(mode, WALK_M_PER_MIN)
        first, second = meters(from_lat, from_lon, to_lat, from_lon), meters(to_lat, from_lon, to_lat, to_lon)
        north, east = to_lat >= from_lat, to_lon >= from_lon
        side = "right" if north == east else "left"  # heading north, east is on your right; heading south, on your left
        points = [
            Step(f"Head {'north' if north else 'south'} on Powell Street for {say_distance(first)}",
                 f"Head {'north' if north else 'south'} on Powell Street", from_lat, from_lon, first, "depart", "Powell Street"),
            Step(f"Turn {side} onto Post Street for {say_distance(second)}", f"Turn {side} onto Post Street",
                 to_lat, from_lon, second, "turn", "Post Street"),
            Step("Arrive", "Arrive", to_lat, to_lon, 0.0, "arrive"),
        ]
        return Route(mode if mode in ROUTERS else "foot", d, d / speed, [p.text for p in points], sample=True, points=points)

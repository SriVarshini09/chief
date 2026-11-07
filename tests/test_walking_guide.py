"""Walking guidance: step locations in routes, what to say next, the /guide routes and Telegram's live location.

Offline: OSRM is a fake fetch and mock mode uses SamplePlaces. The guide is memory only, like your location.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from chief import guide
from chief.guide import Guide, next_cue
from chief.intent import handle_utterance
from chief.places import OpenMap, SamplePlaces, Spot, Step, Whereabouts, meters

NOW = datetime(2026, 10, 21, 8, 0)
UNION = (37.7880, -122.4075)
SAKURA = (37.7880 + 0.0021, -122.4075 + 0.0012)  # the first sample sushi place: north, then east


class FakeNet:
    def __init__(self, route):
        self.route, self.calls = route, []

    def __call__(self, method, url, params):
        self.calls.append((method, url, params))
        return self.route


OSRM = {"code": "Ok", "routes": [{"distance": 1824.0, "duration": 1440.0, "legs": [{"steps": [
    {"maneuver": {"type": "depart", "bearing_after": 92, "location": [-122.4075, 37.7880]}, "name": "Post Street", "distance": 400},
    {"maneuver": {"type": "turn", "modifier": "left", "location": [-122.4030, 37.7880]}, "name": "Hyde Street", "distance": 1200},
    {"maneuver": {"type": "new name", "modifier": "straight", "location": [-122.4030, 37.7960]}, "name": "Hyde Street", "distance": 100},
    {"maneuver": {"type": "roundabout", "exit": 2, "location": [-122.4030, 37.7970]}, "name": "Bay Street", "distance": 120},
    {"maneuver": {"type": "arrive", "modifier": "right", "location": [-122.4040, 37.7975]}, "name": "", "distance": 0},
]}]}]}


# ---- step locations --------------------------------------------------------------------------------------


def test_osrm_steps_keep_their_place_and_distance():
    r = OpenMap(fetch=FakeNet(OSRM), sleep=lambda s: None).get_route(*UNION, 37.7975, -122.4040)
    assert r.steps == ["Head east on Post Street for 400 m", "Turn left onto Hyde Street for 1.2 km",
                       "At the roundabout, take exit 2 onto Bay Street for 120 m", "Arrive on your right"]
    assert [p.text for p in r.points] == r.steps
    assert [(p.lat, p.lon) for p in r.points] == [(37.788, -122.4075), (37.788, -122.403), (37.797, -122.403), (37.7975, -122.404)]
    assert [p.distance_m for p in r.points] == [400, 1300, 120, 0]  # the quiet "new name" stretch adds to Hyde Street
    assert r.points[1].say == "Turn left onto Hyde Street" and r.points[-1].kind == "arrive"


def test_a_route_without_locations_still_reads_but_cannot_be_guided():
    raw = {"code": "Ok", "routes": [{"distance": 10, "duration": 10, "legs": [{"steps": [
        {"maneuver": {"type": "depart"}, "name": "A", "distance": 10}, {"maneuver": {"type": "arrive"}, "name": "", "distance": 0}]}]}]}
    r = OpenMap(fetch=FakeNet(raw), sleep=lambda s: None).get_route(1, 1, 2, 2)
    assert r.steps == ["Head north on A for 10 m", "Arrive"] and not guide.guidable(r.points)


def test_long_routes_keep_every_point_though_the_reading_stops_at_14():
    steps = [{"maneuver": {"type": "depart", "location": [0.1, 0.1]}, "name": "S0", "distance": 50}]
    steps += [{"maneuver": {"type": "turn", "modifier": "left", "location": [0.1, 0.1 + i / 1000]}, "name": f"S{i}", "distance": 50} for i in range(1, 20)]
    steps += [{"maneuver": {"type": "arrive", "location": [0.2, 0.2]}, "name": "", "distance": 0}]
    r = OpenMap(fetch=FakeNet({"code": "Ok", "routes": [{"distance": 1000, "duration": 600, "legs": [{"steps": steps}]}]}),
                sleep=lambda s: None).get_route(0.1, 0.1, 0.2, 0.2)
    assert len(r.steps) == 15 and r.steps[-1] == "Then follow the map the rest of the way"
    assert len(r.points) == 21 and r.points[-1].kind == "arrive"


@pytest.mark.parametrize("to,first,turn", [
    (SAKURA, "Head north on Powell Street", "Turn right onto Post Street"),
    ((37.7846, -122.4046), "Head south on Powell Street", "Turn left onto Post Street"),
    ((37.7846, -122.4100), "Head south on Powell Street", "Turn right onto Post Street"),
])
def test_sample_routes_have_an_l_shape_with_real_corners(to, first, turn):
    r = SamplePlaces().get_route(*UNION, *to)
    assert [p.say for p in r.points] == [first, turn, "Arrive"] and r.steps[-1] == "Arrive"
    assert (r.points[0].lat, r.points[0].lon) == UNION and (r.points[1].lat, r.points[1].lon) == (to[0], UNION[1])
    assert (r.points[2].lat, r.points[2].lon) == to and guide.guidable(r.points)
    assert abs(r.points[0].distance_m - meters(*UNION, to[0], UNION[1])) < 1


def test_the_directions_answer_carries_the_turns_with_places(rt):
    rt.where.set(*UNION, source="browser", now=NOW)
    handle_utterance(rt, "sushi near me")
    p = handle_utterance(rt, "directions to the first one")["proposal"]
    first = p.items[0]
    assert first["title"].startswith("1. Head north on Powell Street")  # the old field is unchanged
    assert first["guide"] is True and (first["lat"], first["lon"]) == UNION and first["kind"] == "depart"
    assert p.items[-1]["title"].endswith("Arrive") and (p.items[-1]["lat"], p.items[-1]["lon"]) == (round(SAKURA[0], 6), round(SAKURA[1], 6))


# ---- what to say next -------------------------------------------------------------------------------------


def walk(a, b, n=20):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def sakura() -> Guide:
    return Guide("Sakura Sushi Bar", *SAKURA, SamplePlaces().get_route(*UNION, *SAKURA).points)


def test_the_first_instruction_is_said_once():
    g = sakura()
    assert next_cue(g, *UNION).say == "Head north on Powell Street for 230 metres."
    assert next_cue(g, *UNION).say == ""


def test_before_a_turn_then_at_it():
    g, corner = sakura(), (SAKURA[0], UNION[1])
    next_cue(g, *UNION)
    said = [c for c in (next_cue(g, *p).say for p in walk(UNION, corner)) if c]
    assert said[0].startswith("In 40 metres, turn right onto Post Street")
    assert said[1] == "Turn right onto Post Street. Then keep going for 110 metres."
    assert len(said) == 2  # each said once


def test_arrival_within_20_metres():
    g, corner = sakura(), (SAKURA[0], UNION[1])
    next_cue(g, *UNION)
    cues = [next_cue(g, *p) for p in walk(UNION, corner) + walk(corner, SAKURA)]
    said = [c.say for c in cues if c.say]
    assert said[-2] == "In 40 metres, you'll reach Sakura Sushi Bar." and said[-1] == "You've arrived at Sakura Sushi Bar."
    arrived = next(c for c in cues if c.status == "arrived")
    assert arrived.say and g.arrived and next_cue(g, *SAKURA).say == ""


def test_the_wrong_way_asks_for_a_new_route():
    g = sakura()
    next_cue(g, *UNION)
    south = [(UNION[0] - i * 0.0001, UNION[1]) for i in range(1, 5)]  # about 11 m a step, away from the turn
    cues = [next_cue(g, *p) for p in south]
    assert not any(c.reroute for c in cues[:2]) and cues[-1].reroute and cues[-1].status == "rerouting"
    assert next_cue(g, UNION[0] - 0.00045, UNION[1]).reroute is False  # not again until another 30 m


def test_taking_the_turn_without_touching_its_point_moves_on_quietly():
    g, corner = sakura(), (SAKURA[0], UNION[1])
    next_cue(g, *UNION)
    next_cue(g, corner[0] - 0.0002, corner[1])  # 22 m short: "In 20 metres..."
    next_cue(g, corner[0] - 0.00018, corner[1] + 0.0002)  # cut the corner, never within 15 m of it
    c = next_cue(g, corner[0] - 0.00005, corner[1] + 0.0004)
    assert not c.reroute and g.nxt == 2


def test_a_poor_fix_is_ignored():
    g = sakura()
    next_cue(g, *UNION)
    assert next_cue(g, UNION[0] - 0.001, UNION[1], accuracy=120).reroute is False


def test_a_long_stretch_hears_keep_going_straight():
    start, turn, end = (37.78, -122.40), (37.79, -122.40), (37.79, -122.399)
    steps = [Step("", "Head north on Market Street", *start, 1110, "depart", "Market Street"),
             Step("", "Turn right onto Pine Street", *turn, 90, "turn", "Pine Street"), Step("", "Arrive", *end, 0, "arrive")]
    g = Guide("Pine Cafe", *end, steps)
    said = [c for c in (next_cue(g, *p).say for p in walk(start, turn, 40)) if c]
    assert said[0] == "Head north on Market Street for 1.1 kilometres."
    assert any(s.startswith("Keep going straight on Market Street. In ") for s in said)


def test_spoken_distances():
    assert guide.spoken_metres(44) == "40 metres" and guide.spoken_metres(3) == "10 metres"
    assert guide.spoken_metres(1000) == "1 kilometre" and guide.spoken_metres(2340) == "2.3 kilometres"


# ---- the session: start, ticks, a new route, stop ----------------------------------------------------------


class Counting(SamplePlaces):
    def __init__(self):
        self.routes = 0

    def get_route(self, *a, **k):
        self.routes += 1
        return super().get_route(*a, **k)


def test_going_the_wrong_way_fetches_a_new_route_but_not_too_often():
    where, tools, clock = Whereabouts(), Counting(), [100.0]
    where.chosen = Spot("Sakura Sushi Bar", *SAKURA)
    assert guide.start(where, tools, *UNION)["say"].startswith("Okay, walking you to Sakura Sushi Bar. Head north")
    outs = [guide.tick(where, tools, UNION[0] - i * 0.0001, UNION[1], clock=lambda: clock[0]) for i in range(1, 5)]
    assert outs[-1]["say"].startswith("This isn't the way, so here's a new route. Head north") and tools.routes == 2
    clock[0] += 5
    for i in range(5, 12):
        guide.tick(where, tools, UNION[0] - i * 0.0001, UNION[1] + 0.0003, clock=lambda: clock[0])
    assert tools.routes == 2  # within 20 seconds of the last one: no new route


def test_no_destination_no_guide():
    out = guide.start(Whereabouts(), SamplePlaces(), *UNION)
    assert out["active"] is False and "directions first" in out["say"]


def test_saying_guide_me_there_starts_and_stop_guiding_stops(rt):
    rt.where.set(*UNION, source="browser", now=NOW)
    handle_utterance(rt, "sushi near me")
    handle_utterance(rt, "directions to the first one")
    out = handle_utterance(rt, "guide me there")
    p = out["proposal"]
    assert p.skill == "guide" and p.needs == "guide" and not p.actions
    assert "Head north on Powell Street" in out["say"] and "Watch the road, not the screen" in out["say"]
    assert rt.where.guide is not None
    out = handle_utterance(rt, "stop guiding")
    assert out["proposal"].needs == "guide_stop" and rt.where.guide is None


def test_guide_me_without_a_location_asks_for_one(rt):
    rt.where.chosen = Spot("Sakura Sushi Bar", *SAKURA)
    assert handle_utterance(rt, "start walking")["proposal"].needs == "location"


def test_forgetting_your_location_forgets_the_guide():
    where = Whereabouts()
    where.chosen = Spot("Sakura Sushi Bar", *SAKURA)
    guide.start(where, SamplePlaces(), *UNION)
    where.clear()
    assert where.guide is None


def test_the_diary_never_keeps_a_guided_walk():
    from chief.gate import WHERE_SKILLS

    assert "guide" in WHERE_SKILLS


# ---- the web routes -----------------------------------------------------------------------------------------


@pytest.fixture
def client(rt):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from chief.web.app import create_app

    return TestClient(create_app(rt, "tok"))


H = {"X-Chief-Token": "tok"}


@pytest.mark.parametrize("path", ["/guide/start", "/guide/tick", "/guide/stop"])
def test_the_guide_routes_need_the_token(client, path):
    assert client.post(path, json={"lat": UNION[0], "lon": UNION[1]}).status_code == 401
    assert client.post(path, json={"lat": UNION[0], "lon": UNION[1]}, headers={"X-Chief-Token": "nope"}).status_code == 401


def test_the_guide_script_needs_the_token(client):
    assert client.get("/guide.js").status_code == 401
    r = client.get("/guide.js", headers=H)
    assert r.status_code == 200 and "Watch the road, not the screen" in r.text and "wakeLock" in r.text


def test_a_walk_through_the_web_routes(client, rt):
    rt.where.chosen = Spot("Sakura Sushi Bar", *SAKURA)
    assert client.post("/guide/start", json={"lat": "x", "lon": 1}, headers=H).status_code == 400
    out = client.post("/guide/start", json={"lat": UNION[0], "lon": UNION[1], "to": {"lat": SAKURA[0], "lon": SAKURA[1]}}, headers=H).json()
    assert out["active"] and out["to"] == "Sakura Sushi Bar"
    corner = (SAKURA[0], UNION[1])
    said = [client.post("/guide/tick", json={"lat": a, "lon": b, "accuracy": 5}, headers=H).json() for a, b in walk(UNION, corner) + walk(corner, SAKURA)]
    assert said[-1]["status"] == "arrived" or any(s["status"] == "arrived" for s in said)
    assert rt.where.guide is None and rt.where.here(NOW) is not None
    assert client.post("/guide/tick", json={"lat": SAKURA[0], "lon": SAKURA[1]}, headers=H).json()["active"] is False
    assert client.post("/guide/stop", headers=H).json()["active"] is False


def test_the_pages_load_the_guide():
    from pathlib import Path

    web = Path(__file__).parent.parent / "chief" / "web"
    for page in ("index.html", "dashboard.html"):
        text = (web / page).read_text(encoding="utf-8")
        assert "/guide.js" in text and "ChiefGuide.offer(" in text and "ChiefGuide.handle(" in text


# ---- Telegram: the next instruction near each turn, from a live location -----------------------------------


def test_telegram_live_location_speaks_the_turns(rt):
    from chief.telegram_app import Bot

    class Api:
        def __init__(self):
            self.calls = []

        def call(self, method, **params):
            self.calls.append((method, params))
            return {}

    api = Api()
    bot = Bot(api, 111).attach(rt)
    rt.where.chosen = Spot("Sakura Sushi Bar", *SAKURA)
    guide.start(rt.where, rt.tools, *UNION)
    corner = (SAKURA[0], UNION[1])
    for lat, lon in walk(UNION, corner) + walk(corner, SAKURA):
        bot.handle({"update_id": 2, "edited_message": {"from": {"id": 111}, "chat": {"id": 111, "type": "private"},
                                                         "location": {"latitude": lat, "longitude": lon}}})
    texts = [p.get("text", "") for m, p in api.calls if m == "sendMessage"]
    assert any(t.startswith("In 40 metres, turn right onto Post Street") for t in texts)
    assert texts[-1] == "You've arrived at Sakura Sushi Bar."

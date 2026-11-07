"""Places near you, directions and booking. NOW is Wednesday 2026-10-21, 08:00 (tests/conftest.py).

Offline: mock mode uses chief.places.SamplePlaces, and OpenMap is tested with a fake fetch, so nothing here
touches the network. The rules that matter most: your location stays in memory, nothing is booked or paid by
Chief, and the only write (putting a booking on the calendar) waits for approval like everything else.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from chief.intent import handle_utterance
from chief.places import (
    OpenMap, PlacesOff, Spot, Whereabouts, directions_link, open_now, safe_phone, safe_url, say_distance, want_from,
)
from chief.tools.base import PLACE_TOOLS, READ_TOOLS, WRITE_TOOLS

NOW = datetime(2026, 10, 21, 8, 0)  # a Wednesday
OWNER = 111


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return [] if method == "getUpdates" else {}

    def named(self, method):
        return [p for m, p in self.calls if m == method]


def message(text, sender=OWNER):
    return {"update_id": 1, "message": {"from": {"id": sender}, "chat": {"id": sender, "type": "private"}, "text": text}}
UNION = (37.7880, -122.4075)


def ask(rt, text):
    return handle_utterance(rt, text)


def writes(tools):
    return [c for c in tools.calls if c[0] in WRITE_TOOLS]


@pytest.fixture
def placed(rt):
    """Chief knows you're at Union Square, as if the browser had shared it."""
    rt.where.set(*UNION, source="browser", now=NOW)
    return rt


# ---- reading opening hours ----------------------------------------------------------------------------


@pytest.mark.parametrize("hours,when,expected", [
    ("24/7", NOW, True),
    ("Mo-Su 07:00-22:00", NOW, True),
    ("Mo-Fr 09:00-17:00", NOW, False),
    ("Mo-Fr 09:00-17:00", NOW.replace(hour=12), True),
    ("Mo-Sa 12:00-15:00,17:30-22:30; Su off", NOW.replace(hour=16), False),
    ("Mo-Sa 12:00-15:00,17:30-22:30; Su off", NOW.replace(hour=18), True),
    ("Mo-Su 10:00-22:00; We off", NOW.replace(hour=12), False),           # a later rule replaces an earlier one
    ("Tu 18:00-02:00", NOW.replace(hour=1), True),                          # Tuesday night, still open after midnight
    ("Sa-Mo 10:00-20:00", NOW.replace(hour=12), False),                     # wraps the week: Sat, Sun, Mon
    ("Mo-Fr 08:00-12:00; PH off", NOW, None),                               # holidays: too fancy, so say nothing
    ("Jan-Mar Mo-Fr 09:00-17:00", NOW, None),
    ("", NOW, None),
    ("sunrise-sunset", NOW, None),
])
def test_open_now(hours, when, expected):
    assert open_now(hours, when) is expected


@pytest.mark.parametrize("text,say,filtered", [
    ("sushi", "sushi places", True), ("good sushi restaurants", "sushi places", True), ("a pharmacy", "pharmacies", True),
    ("coffee", "cafes", True), ("south indian food", "South Indian places", True), ("an atm", "ATMs", True),
    ("starbucks", 'places called "starbucks"', False), ("", "places to eat", True),
])
def test_what_you_asked_for(text, say, filtered):
    want = want_from(text)
    assert want.say == say and bool(want.filters) is filtered


def test_links_from_map_data_are_only_plain_web_links():
    assert safe_url("example.com/menu") == "https://example.com/menu"
    assert safe_url("https://a.example;https://b.example") == "https://a.example"
    for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x.example", "https://x.example/a b", 'https://x"y'):
        assert safe_url(bad) == ""
    assert safe_phone("+1 (555) 010-0200; +1 555 999") == "+15550100200"
    assert safe_phone("call us") == "" and safe_phone("1+2345678") == ""


def test_distances_and_the_map_link():
    assert say_distance(4) == "10 m" and say_distance(347) == "350 m" and say_distance(1830) == "1.8 km" and say_distance(2000) == "2 km"
    link = directions_link(37.79, -122.41, "car")
    assert link.startswith("https://www.google.com/maps/dir/?api=1") and "travelmode=driving" in link
    assert "origin" not in link  # the phone starts from where it is; Chief doesn't put you in the link


# ---- where you are ------------------------------------------------------------------------------------


def test_a_location_goes_stale_and_can_be_forgotten():
    w = Whereabouts()
    w.set(*UNION, source="browser", now=NOW)
    assert w.here(NOW + timedelta(hours=1)) is not None
    assert w.here(NOW + timedelta(hours=7)) is None  # this morning's location is not where you are now
    w.set(*UNION, now=NOW)
    w.clear()
    assert w.here(NOW) is None and w.spots == [] and w.chosen is None


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, 181), (0, 0), ("x", 1)])
def test_nonsense_locations_are_refused(lat, lon):
    with pytest.raises((ValueError, TypeError)):
        Whereabouts().set(lat, lon)


def test_location_is_never_written_to_disk(rt, cfg):
    ask(rt, "I'm at Union Square")
    ask(rt, "sushi near me")
    saved = b"".join(p.read_bytes() for p in cfg.state_dir.rglob("*") if p.is_file())
    assert b"37.78" not in saved and b"-122.4" not in saved


# ---- nearby -------------------------------------------------------------------------------------------


def test_without_a_location_chief_asks_for_one(rt, tools):
    out = ask(rt, "find sushi near me")
    p = out["proposal"]
    assert p.skill == "nearby" and p.needs == "location" and out["say"].startswith("Can I use your live location?") and not p.actions


def test_saying_where_you_are_sets_it(rt):
    out = ask(rt, "I'm at Union Square")
    assert out["say"].startswith("Got it, you're at Union Square")
    assert rt.where.here(NOW).source == "said"


def test_sushi_near_me(placed, tools):
    out = ask(placed, "any good sushi restaurants near me?")
    p = out["proposal"]
    assert p.skill == "nearby" and not p.actions and writes(tools) == []
    assert out["say"].startswith("Here are the 4 closest sushi places near you. The closest is Sakura Sushi Bar, 260 m away")
    assert "directions to the first one" in out["say"] and "sample places" in out["say"]
    first = p.items[0]
    assert first["title"] == "1. Sakura Sushi Bar" and "Closed now" in first["note"]  # 11am opening, it's 8am
    assert [link["label"] for link in first["links"]] == ["Directions", "Website", "Call"]
    assert "OpenStreetMap" in p.sources[0]
    assert [s.name for s in placed.where.spots][:2] == ["Sakura Sushi Bar", "Koi Sushi & Ramen"]


def test_i_am_at_a_place_and_want_coffee_in_one_go(rt):
    out = ask(rt, "I'm at Union Square, find me a coffee place")
    assert out["say"].startswith("Got it, you're at Union Square") and "cafes near Union Square" in out["say"]


def test_near_a_named_place_searches_there_without_moving_you(placed):
    out = ask(placed, "restaurants near the Ferry Building")
    assert "near The Ferry Building" in out["say"]
    assert placed.where.here(NOW).source == "browser"


def test_forget_my_location(placed):
    assert ask(placed, "forget my location")["say"] == "Done. I've forgotten your location."
    assert placed.where.here(NOW) is None


# ---- directions ---------------------------------------------------------------------------------------


def test_directions_to_the_first_one(placed):
    ask(placed, "sushi near me")
    out = ask(placed, "directions to the first one")
    p = out["proposal"]
    assert p.skill == "directions" and not p.actions
    assert out["say"].startswith("Sakura Sushi Bar (120 Sample Street) is 330 m: about 4 minutes on foot.")
    assert "Head north on Powell Street" in out["say"] and p.links[0]["label"] == "Directions"
    assert p.items[-1]["title"].endswith("Arrive")


def test_by_bike_to_number_three(placed):
    ask(placed, "sushi near me")
    out = ask(placed, "how do I get to number 3 by bike")
    assert out["say"].startswith("Hana Japanese Kitchen") and "by bike" in out["say"]
    assert "travelmode=bicycling" in out["proposal"].links[0]["url"]


def test_buses_and_trains_go_to_the_map(placed):
    out = ask(placed, "how do I get to Golden Gate Park by bus")
    assert "can't see live bus or train times" in out["say"] and "travelmode=transit" in out["proposal"].links[0]["url"]


def test_from_one_place_to_another(rt):
    out = ask(rt, "directions from Union Square to the Ferry Building")
    assert "from Union Square, San Francisco" in out["say"] and "origin=" in out["proposal"].links[0]["url"]


def test_directions_without_a_location_still_give_the_map(rt):
    out = ask(rt, "how far is the Ferry Building")
    p = out["proposal"]
    assert p.needs == "location" and p.links[0]["label"] == "Directions" and "Share your location" in out["say"]


def test_a_route_that_fails_falls_back_honestly(placed, tools):
    def broken(*a, **k):
        raise TimeoutError("router busy")

    tools._places.get_route = broken
    out = ask(placed, "directions to the Ferry Building")
    assert "couldn't work out the route" in out["say"] and "as the crow flies" in out["say"]


# ---- booking: Chief gets it ready, you book and pay ---------------------------------------------------


def test_book_a_table_only_adds_to_the_calendar_after_approval(placed, tools):
    ask(placed, "sushi near me")
    out = ask(placed, "book a table at the second one for 4 at 8pm")
    p = out["proposal"]
    assert out["say"].startswith("Ready for Koi Sushi & Ramen: a table for 4, today at 8:00 PM.")
    assert "can't make the booking or pay for you" in out["say"]
    assert [link["label"] for link in p.links] == ["Reserve", "Call", "Directions"]
    assert p.links[0]["url"].startswith("https://example.com/")
    action = p.actions[0]
    assert action.tool == "create_event" and action.args["title"] == "Dinner at Koi Sushi & Ramen"
    assert action.args["start"] == "2026-10-21T20:00:00" and action.args["end"] == "2026-10-21T21:30:00"
    assert "Not booked by Chief" in action.args["description"]
    assert writes(tools) == []
    ask(placed, "do it")
    assert [c[0] for c in writes(tools)] == ["create_event"]


def test_party_size_in_words_and_a_guess_is_said_out_loud(placed):
    ask(placed, "sushi near me")
    assert "a table for 2 (say another number" in ask(placed, "book the first one at 7pm")["say"]
    assert "a table for 3," in ask(placed, "reserve a table at the first one for three people at 7:30pm")["say"]


def test_a_booking_without_a_time_asks_and_changes_nothing(placed, tools):
    ask(placed, "sushi near me")
    out = ask(placed, "book a table at the first one")
    assert out["say"].startswith("What time should I plan Sakura Sushi Bar for") and not out["proposal"].actions
    assert out["proposal"].links[0]["label"] == "Reserve"


def test_a_booking_without_a_place_asks(rt):
    assert ask(rt, "book a table for 2 at 7pm")["say"].startswith("Which place should I get ready?")


def test_tickets(placed, tools):
    out = ask(placed, "book tickets for Dune tomorrow at 7pm")
    p = out["proposal"]
    assert "can't make the booking or pay for you" in out["say"] and p.links[0]["label"] == "Find tickets"
    assert p.actions[0].args["title"] == "Dune" and p.actions[0].args["start"] == "2026-10-22T19:00:00"
    assert writes(tools) == []


def test_place_skills_cannot_write_and_only_read_maps(rt):
    for name in ("nearby", "directions", "booking"):
        skill = rt.skills[name]
        assert skill.allowed_tools <= READ_TOOLS
        assert skill.propose_tools <= {"create_event"}
    assert PLACE_TOOLS <= READ_TOOLS


@pytest.mark.parametrize("text,skill", [
    ("what's on my calendar today?", "agenda"), ("remind me to call mom at 6pm", "calendar_edit"),
    ("move my 3pm to tomorrow", "calendar_edit"), ("find me 30 minutes for the gym tomorrow", "calendar_edit"),
    ("is there a pharmacy near me", "nearby"), ("where's the nearest atm", "nearby"), ("take me there", "directions"),
    ("book a table for 2", "booking"),
])
def test_the_new_skills_do_not_steal_old_questions(rt, text, skill):
    from chief.intent import _match_skill, _norm

    assert _match_skill(rt, _norm(text)) == skill


# ---- the real provider, with a fake network -----------------------------------------------------------


class FakeNet:
    def __init__(self, overpass=None, nominatim=None, photon=None, route=None, fail=()):
        self.answers = {"overpass": overpass, "nominatim": nominatim or [], "photon": photon or {"features": []}, "route": route}
        self.fail, self.calls = set(fail), []

    def __call__(self, method, url, params):
        name = "overpass" if "overpass" in url else "nominatim" if "nominatim" in url else "photon" if "photon" in url else "route"
        self.calls.append((name, url, params))
        if name in self.fail:
            raise TimeoutError(f"{name} is busy")
        return self.answers[name]


OVERPASS = {"elements": [
    {"type": "node", "lat": 37.7889, "lon": -122.4070, "tags": {"name": "Far Sushi", "cuisine": "sushi", "amenity": "restaurant"}},
    {"type": "way", "center": {"lat": 37.7882, "lon": -122.4074}, "tags": {
        "name": "Near Sushi", "cuisine": "japanese;sushi", "opening_hours": "24/7", "website": "nearsushi.example",
        "phone": "+1 555 0100", "addr:housenumber": "1", "addr:street": "Post Street", "amenity": "restaurant"}},
    {"type": "node", "lat": 37.7882, "lon": -122.4074, "tags": {"cuisine": "sushi"}},  # no name: skipped
]}


def test_overpass_finds_places_closest_first_and_rounds_your_location():
    net = FakeNet(overpass=OVERPASS)
    spots = OpenMap(fetch=net, sleep=lambda s: None).find_places("sushi", 37.788049, -122.407531)
    assert [s.name for s in spots] == ["Near Sushi", "Far Sushi"]
    near = spots[0]
    assert near.cuisine == "japanese, sushi" and near.website == "https://nearsushi.example" and near.phone == "+15550100"
    assert near.address == "1 Post Street"
    query = net.calls[0][2]["data"]
    assert "37.788,-122.408" in query and "37.78804" not in query  # about 100 m, never the exact point
    assert '"cuisine"~"sushi|japanese"' in query


def test_a_busy_overpass_falls_back_to_nominatim_and_rests():
    nominatim = [{"name": "Name Sushi", "lat": "37.789", "lon": "-122.407", "type": "restaurant",
                  "extratags": {"opening_hours": "Mo-Su 11:00-22:00"}, "address": {"road": "Geary Street", "house_number": "9"}}]
    net = FakeNet(nominatim=nominatim, fail={"overpass"})
    clock = [100.0]
    om = OpenMap(fetch=net, sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
    assert [s.name for s in om.find_places("sushi", *UNION)] == ["Name Sushi"]
    assert om.find_places("sushi", *UNION)[0].address == "9 Geary Street"
    assert [c[0] for c in net.calls] == ["overpass", "nominatim", "nominatim"]  # Overpass rests for ten minutes
    assert clock[0] >= 101.0  # Nominatim asks for at most one request a second


def test_photon_is_the_last_resort():
    photon = {"features": [{"properties": {"name": "Photon Sushi", "street": "Mason Street", "osm_value": "restaurant"},
                            "geometry": {"coordinates": [-122.4080, 37.7885]}}]}
    net = FakeNet(overpass={"elements": []}, photon=photon)
    assert [s.name for s in OpenMap(fetch=net, sleep=lambda s: None).find_places("sushi", *UNION)] == ["Photon Sushi"]


def test_find_location_prefers_places_near_you():
    net = FakeNet(nominatim=[{"name": "Ferry Building", "lat": "37.7955", "lon": "-122.3937", "display_name": "Ferry Building, Embarcadero, San Francisco"}])
    spot = OpenMap(fetch=net, sleep=lambda s: None).find_location("ferry building", *UNION)
    assert spot.name == "Ferry Building" and spot.address == "Embarcadero, San Francisco"
    assert "viewbox" in net.calls[0][2] and "37.788" not in net.calls[0][2]["viewbox"]


def test_a_route_reads_as_plain_turns():
    route = {"code": "Ok", "routes": [{"distance": 1824.0, "duration": 1440.0, "legs": [{"steps": [
        {"maneuver": {"type": "depart", "bearing_after": 92}, "name": "Post Street", "distance": 400},
        {"maneuver": {"type": "turn", "modifier": "left"}, "name": "Hyde Street", "distance": 1200},
        {"maneuver": {"type": "new name", "modifier": "straight"}, "name": "Hyde Street", "distance": 100},
        {"maneuver": {"type": "roundabout", "exit": 2}, "name": "Bay Street", "distance": 120},
        {"maneuver": {"type": "arrive", "modifier": "right"}, "name": "", "distance": 0},
    ]}]}]}
    net = FakeNet(route=route)
    r = OpenMap(fetch=net, sleep=lambda s: None).get_route(37.788049, -122.407531, 37.80, -122.42, "foot")
    assert r.mode == "foot" and r.minutes == 24 and r.distance_m == 1824
    assert r.steps == ["Head east on Post Street for 400 m", "Turn left onto Hyde Street for 1.2 km",
                       "At the roundabout, take exit 2 onto Bay Street for 120 m", "Arrive on your right"]
    url = net.calls[0][1]
    assert "routed-foot" in url and "-122.4075,37.788" in url and "37.788049" not in url


def test_no_route_is_an_error_not_a_guess():
    with pytest.raises(LookupError):
        OpenMap(fetch=FakeNet(route={"code": "NoRoute", "routes": []}), sleep=lambda s: None).get_route(1, 1, 2, 2)


def test_places_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CHIEF_PLACES", "off")
    net = FakeNet()
    with pytest.raises(PlacesOff):
        OpenMap(fetch=net).find_places("sushi", *UNION)
    assert net.calls == []


# ---- the activity log and the demo cache never keep where you are -------------------------------------


def test_the_activity_log_keeps_only_a_rough_point(placed):
    ask(placed, "sushi near me")
    events = [e for e in placed.bus.since(0) if e.tool == "find_places"]
    text = repr([e.detail for e in events])
    assert events and "37.79" in text and "37.788" not in text


def test_recording_the_demo_never_caches_places(tmp_path, tools):
    from chief.tools.replay import DemoCache, RecorderTools

    cache = DemoCache(tmp_path / "cache.json")
    rec = RecorderTools(tools, cache, allow_writes=False)
    assert rec.find_places("sushi", *UNION)
    assert len(cache) == 0


# ---- the web page and Telegram ------------------------------------------------------------------------


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def client(rt):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from chief.web.app import create_app

    return TestClient(create_app(rt, "tok"))


H = {"X-Chief-Token": "tok"}


def test_the_page_shares_and_forgets_a_location(client, rt):
    assert client.post("/location", json={"lat": 37.788, "lon": -122.4075}).status_code == 401
    assert client.post("/location", json={"lat": "north", "lon": 1}, headers=H).status_code == 400
    assert client.post("/location", json={"lat": 37.788, "lon": -122.4075}, headers=H).json() == {"ok": True, "source": "browser"}
    assert client.get("/status", headers=H).json()["location"] is True
    out = client.post("/ask", json={"text": "sushi near me"}, headers=H).json()
    assert out["proposal"]["items"][0]["title"] == "1. Sakura Sushi Bar" and out["proposal"]["needs"] == ""
    assert client.delete("/location", headers=H).json() == {"ok": True}
    assert client.post("/ask", json={"text": "sushi near me"}, headers=H).json()["proposal"]["needs"] == "location"


def test_telegram_asks_for_the_location_then_answers(api, cfg, tools):
    from chief.runtime import Runtime
    from chief.telegram_app import Bot

    bot = Bot(api, OWNER)
    bot.attach(Runtime(cfg, tools=tools, notify=bot.notify))
    bot.handle(message("sushi near me"))
    keyboard = api.named("sendMessage")[-1]["reply_markup"]
    assert keyboard["keyboard"][0][0]["request_location"] is True
    bot.handle({"update_id": 7, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "location": {"latitude": 37.788, "longitude": -122.4075}}})
    last = api.named("sendMessage")[-1]
    assert last["text"].startswith("Here are the 4 closest sushi places near you")
    rows = last["reply_markup"]["inline_keyboard"]
    urls = [b["url"] for row in rows for b in row]
    assert urls and all(u.startswith("https://") for u in urls)  # Telegram can't open tel: links in buttons
    assert "1. Sakura Sushi Bar" in last["text"]


def test_a_stranger_cannot_set_the_location(api, cfg, tools):
    from chief.runtime import Runtime
    from chief.telegram_app import Bot

    bot = Bot(api, OWNER)
    bot.attach(Runtime(cfg, tools=tools, notify=bot.notify))
    bot.handle({"update_id": 8, "message": {"from": {"id": 999}, "chat": {"id": 999}, "location": {"latitude": 1.0, "longitude": 1.0}}})
    assert bot.rt.where.here(NOW) is None and api.calls == []


def test_a_booking_card_on_telegram_has_reserve_and_approve(api, cfg, tools):
    from chief.runtime import Runtime
    from chief.telegram_app import Bot

    bot = Bot(api, OWNER)
    bot.attach(Runtime(cfg, tools=tools, notify=bot.notify))
    bot.rt.where.set(*UNION, source="telegram", now=NOW)
    bot.handle(message("sushi near me"))
    bot.handle(message("book a table at the first one for 2 at 7pm"))
    last = api.named("sendMessage")[-1]
    labels = [b["text"] for row in last["reply_markup"]["inline_keyboard"] for b in row]
    assert labels[:2] == ["Reserve", "Directions"] and labels[-2:] == ["Approve", "Skip"]
    assert "Phone: +155501000" in last["text"]


def test_a_place_with_a_comma_is_one_place(rt):
    """Found in the live test: "Union Square, San Francisco" was cut at the comma and went to New York."""
    out = ask(rt, "I'm at Union Square, San Francisco")
    assert out["say"] == "Got it, you're at Union Square, San Francisco. Ask me what's nearby, or how to get somewhere."


def test_a_place_then_a_search_after_the_comma(rt):
    out = ask(rt, "I'm at Union Square, sushi near me")
    assert "sushi places near Union Square" in out["say"]


def test_one_result_reads_naturally(placed, tools):
    tools._places.find_places = lambda *a, **k: [Spot("Only Sushi", 37.789, -122.4075, distance_m=110)]
    assert ask(placed, "sushi near me")["say"].startswith("I found one near you: Only Sushi, 110 m away")


def test_a_looked_up_place_is_named_by_its_town():
    net = FakeNet(nominatim=[{"name": "Union Square", "lat": "37.788", "lon": "-122.4075",
                              "display_name": "Union Square, Community Board 5, Manhattan, New York",
                              "address": {"city": "San Francisco", "state": "California"}}])
    assert OpenMap(fetch=net, sleep=lambda s: None).find_location("union square").address == "San Francisco"


def test_telegram_live_location_updates_quietly(api, cfg, tools):
    from chief.runtime import Runtime
    from chief.telegram_app import Bot

    bot = Bot(api, OWNER)
    bot.attach(Runtime(cfg, tools=tools, notify=bot.notify))
    bot.handle(message("sushi near me"))
    assert "Share My Live Location" in api.named("sendMessage")[-1]["text"]
    sent = len(api.named("sendMessage"))
    moved = {"update_id": 9, "edited_message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "location": {"latitude": 37.79, "longitude": -122.41}}}
    bot.handle(moved)
    assert round(bot.rt.where.here(NOW).lat, 2) == 37.79 and len(api.named("sendMessage")) == sent  # no reply to every step
    stranger = {"update_id": 10, "edited_message": {"from": {"id": 999}, "chat": {"id": 999}, "location": {"latitude": 1.0, "longitude": 1.0}}}
    bot.handle(stranger)
    assert round(bot.rt.where.here(NOW).lat, 2) == 37.79


@pytest.mark.parametrize("broken,question,service", [
    ("find_places", "sushi near me", "the map service"),
    ("list_events", "what's on my calendar today?", "your calendar"),
    ("fetch_emails", "what's in my inbox", "your email"),
])
def test_an_outage_is_a_plain_answer_not_a_crash(placed, tools, broken, question, service):
    """Found in the launch check: one slow service made every answer 'Sorry, something went wrong.'"""
    def down(*a, **k):
        raise TimeoutError("secret-looking detail sk-123")

    target = tools._places if broken == "find_places" else tools
    setattr(target, broken, down)
    out = ask(placed, question)
    assert out["say"].startswith(f"I couldn't reach {service} just now") and "Nothing was changed" in out["say"]
    assert "sk-123" not in out["say"] and out["steps"][-1] == "Failed"


def test_places_answers_are_not_written_to_the_state_folder(placed, cfg, tools):
    """Found in the security review: the activity log kept 'Sakura Sushi Bar, 260 m away'."""
    ask(placed, "sushi near me")
    ask(placed, "directions to the first one")
    ask(placed, "book a table at the second one for 4 at 8pm")
    ask(placed, "do it")
    saved = "".join(p.read_text("utf-8") for p in cfg.state_dir.rglob("*") if p.is_file())
    assert "Sakura" not in saved and "Koi" not in saved and " m away" not in saved

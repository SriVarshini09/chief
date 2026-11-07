"""The garden dashboard: real data, the same token, and no way to change anything by reading it."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from chief.dashboard import opening, snapshot  # noqa: E402
from chief.tools.base import WRITE_TOOLS  # noqa: E402
from chief.web.app import DASHBOARD_PAGE, FOOTAGE, FOOTAGE_FILES, PET_SCRIPT, WORLD_SCRIPT, create_app  # noqa: E402

TOKEN = "s3cret-test-token"
H = {"X-Chief-Token": TOKEN}


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt, TOKEN))


def test_both_routes_need_the_token(client):
    for path in ("/dashboard", "/dashboard/data", "/dashboard/world.js", "/dashboard/pet.js"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Chief-Token": "nope"}).status_code == 401


def test_the_page_is_served_fresh(client):
    r = client.get("/dashboard", headers=H)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and r.headers["cache-control"] == "no-store"


def test_the_snapshot_shows_the_real_day(rt):
    data = snapshot(rt)
    assert data["date"] == "Wednesday, October 21" and data["hour"] == 8 and data["mode"] == "mock"
    assert [m["clock"] for m in data["today"]][0] == "10:00 AM" and len(data["today"]) == 3
    assert len(data["week"]) == 7 and data["week"][0]["day"] == "Wed"
    assert next(d for d in data["week"] if d["day"] == "Thu")["heavy"] is True
    assert [m["from"] for m in data["inbox"]["reply"]] == ["Sam", "Dana"]
    assert data["inbox"]["flagged"] == 1 and data["missing"] == []


def test_chief_speaks_first_from_the_real_day(rt):
    say = snapshot(rt)["say"]
    assert say.startswith("Hi, good morning! I'm glad you're here. You've got 3 meetings today, starting with Standup at 10:00 AM.")
    assert "Sam and Dana are waiting to hear from you." in say
    assert "I ignored 1 email that tried to give me orders." in say


def test_chief_says_so_when_it_cannot_see_something(rt, tools):
    def down(*a, **k):
        raise RuntimeError("down")

    tools.fetch_emails = down
    assert "I can't reach your email right now." in snapshot(rt)["say"]


def test_a_quiet_day_sounds_quiet():
    mail = {"reply": [], "deadline": [], "fyi": 4, "flagged": 0}
    say = opening(14.5, [], mail, [], [])
    assert say == "Hi, good afternoon! I'm glad you're here. Your calendar's clear today. And nothing in your inbox needs you. I'll be right here whenever you need me."


def test_a_heavy_day_ahead_is_mentioned():
    days = [{"full": "Wednesday", "heavy": False}, {"full": "Thursday", "heavy": True}]
    mail = {"reply": [], "deadline": [], "fyi": 0, "flagged": 0}
    assert "Thursday looks heavy" in opening(9, [], mail, days, [])


def test_many_senders_are_summed_up():
    mail = {"reply": [{"from": n} for n in ("Ana", "Bo", "Cy", "Di")], "deadline": [], "fyi": 0, "flagged": 0}
    assert "Ana, Bo and 2 more are waiting" in opening(9, [], mail, [], [])


def test_the_page_uses_real_states_and_no_invented_voice():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "data.say" in page and "innerHTML" not in page
    assert page.count('data-mood') >= 1 and 'setMood("waiting")' in page and 'setMood("thinking")' in page


def test_sleep_is_labelled_as_a_sample(rt):
    rest = snapshot(rt)["rest"]
    assert rest["sample"] is True and rest["nights"] and rest["average"] > 0


def test_mail_bodies_and_the_injection_text_never_reach_the_page(rt):
    text = str(snapshot(rt))
    assert "attacker" not in text and "Ignore all previous" not in text and "Can you send the deck" not in text


def test_reading_it_writes_nothing_and_is_not_in_the_activity_stream(rt, tools, client):
    before = rt.bus.last_seq
    client.get("/dashboard/data", headers=H)
    assert not [c for c in tools.calls if c[0] in WRITE_TOOLS]
    assert rt.bus.last_seq == before


def test_one_service_failing_leaves_the_rest_standing(rt, tools):
    def down(*a, **k):
        raise RuntimeError("mail is down")

    tools.fetch_emails = down
    data = snapshot(rt)
    assert data["missing"] == ["mail"] and data["inbox"]["reply"] == []
    assert len(data["today"]) == 3


def test_an_approval_from_the_dashboard_goes_through_the_gate(client, tools):
    pid = client.post("/ask", json={"text": "how's my week looking?"}, headers=H).json()["proposal"]["id"]
    assert client.post(f"/approve/{pid}", headers=H).json()["results"]
    assert [c for c in tools.calls if c[0] in WRITE_TOOLS]


def test_the_page_never_builds_html_from_text():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "innerHTML" not in page and "outerHTML" not in page and "insertAdjacentHTML" not in page


def test_the_page_respects_reduced_motion_and_loads_nothing_from_outside():
    page = DASHBOARD_PAGE.read_text("utf-8")
    world = WORLD_SCRIPT.read_text("utf-8")
    assert "prefers-reduced-motion" in page
    links = page.replace('href="https://commons.wikimedia.org', "").replace('href="https://creativecommons.org', "")
    assert "https://" not in links and 'src="http' not in page
    assert "http" not in world and "fetch(" not in world and "XMLHttpRequest" not in world


def test_the_kitten_is_served_and_needs_nothing_from_outside(client):
    r = client.get("/dashboard/pet.js", headers=H)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "ChiefPet" in r.text
    script = PET_SCRIPT.read_text("utf-8")
    assert "http" not in script and "fetch(" not in script and "if (!gl) return null;" in script


def test_the_kitten_walks_with_purpose_and_its_mouth_follows_speech():
    script = PET_SCRIPT.read_text("utf-8")
    assert "STRIDE_LENGTH" in script and "function chooseRoutine()" in script and "word(text, ms)" in script
    assert "nextBeat" not in script


def test_the_kitten_only_speaks_and_listens_when_you_let_it():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert 'addEventListener("pointerdown", firstTouch' in page  # silent until you touch the page
    assert "onboundary" in page  # words appear as they are spoken
    assert 'id="voice"' in page and "chief-voice" in page  # and it can be turned off
    assert 'ear.start();' in page and 'getUserMedia' not in page  # the mic opens only from the button


def test_the_kitten_guides_you_through_the_page():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "async function tour()" in page and 'id="tourBtn"' in page and 'id="skipTour"' in page
    assert "guideTo(card," in page and "pet.goTo(" in page and "chief-toured" in page


def test_the_kitten_looks_away_from_secrets_and_never_repeats_them():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "pet.shy(secret)" in page and '"(hidden)"' in page
    assert 'e.target.type === "password"' in page


def test_you_can_pick_the_scene_and_it_is_remembered():
    page = DASHBOARD_PAGE.read_text("utf-8")
    for scene in ("auto", "picture", "meadow", "daisies", "sunset", "orchard"):
        assert f'data-scene="{scene}"' in page
    assert "chief-scene" in page and 'id="scenePicture"' in page


def test_icons_are_drawn_in_the_page():
    page = DASHBOARD_PAGE.read_text("utf-8")
    for name in ("mic", "speaker", "mute", "undo", "calendar", "mail", "check", "compass"):
        assert f'id="i-{name}"' in page
    assert 'rel="icon" href="data:image/svg+xml' in page


def test_the_world_script_is_served_as_javascript(client):
    r = client.get("/dashboard/world.js", headers=H)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "ChiefWorld" in r.text


@pytest.mark.parametrize("name", sorted(FOOTAGE_FILES))
def test_footage_needs_the_token_and_is_served(client, name):
    assert client.get(f"/dashboard/footage/{name}").status_code == 401
    r = client.get(f"/dashboard/footage/{name}", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == FOOTAGE_FILES[name] and len(r.content) > 1000


def test_video_answers_range_requests_for_iphones(client):
    r = client.get("/dashboard/footage/daisies.mp4", headers={**H, "Range": "bytes=0-1023"})
    assert r.status_code == 206 and len(r.content) == 1024
    assert r.headers["content-range"].startswith("bytes 0-1023/")


@pytest.mark.parametrize("name", ["CREDITS.md", "..%2Fapp.py", "other.mp4"])
def test_only_the_listed_footage_is_served(client, name):
    assert client.get(f"/dashboard/footage/{name}", headers=H).status_code == 404


def test_footage_resumes_after_the_page_comes_back_and_stays_still_for_reduced_motion():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert '$("footage").play()' in page  # phones pause background video and do not restart it
    assert 'removeAttribute("autoplay")' in page


def test_your_own_picture_is_optional_and_stays_out_of_git(client, monkeypatch, tmp_path):
    import subprocess

    import chief.web.app as web

    monkeypatch.setattr(web, "FOOTAGE", tmp_path)
    assert client.get("/dashboard/footage/background.jpg", headers=H).status_code == 404  # a fresh clone falls back
    (tmp_path / "background.jpg").write_bytes(b"\xff\xd8picture")
    assert client.get("/dashboard/footage/background.jpg").status_code == 401
    assert client.get("/dashboard/footage/background.jpg", headers=H).content == b"\xff\xd8picture"
    ignored = subprocess.run(["git", "check-ignore", "chief/web/footage/background.jpg"], capture_output=True, text=True)
    if ignored.returncode == 128:  # not a git checkout (a downloaded zip): read the rule instead
        assert "chief/web/footage/background.jpg" in open(".gitignore", encoding="utf-8").read()
        return
    assert ignored.returncode == 0


def test_every_clip_is_credited_with_its_source_and_license():
    credits = (FOOTAGE / "CREDITS.md").read_text("utf-8")
    for name in FOOTAGE_FILES:
        assert name in credits
    assert "Pixelmaniac pictures" in credits and "Bureau of Land Management" in credits
    assert credits.count("commons.wikimedia.org/wiki/File:") == 3 and "ublic domain" in credits


def test_the_orchard_photo_is_credited_on_the_page_and_says_it_was_changed():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "Savannah River Site" in page and "CC BY 2.0" in page and "creativecommons.org/licenses/by/2.0" in page
    assert "recoloured" in page


def test_outside_links_never_carry_the_token_in_a_referrer():
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert page.count('target="_blank"') == page.count('rel="noopener noreferrer"') == 2


def test_a_device_without_webgl_keeps_the_drawn_garden():
    world = WORLD_SCRIPT.read_text("utf-8")
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "if (!gl) return null;" in world
    assert "else renderDrawnScene(t, dt);" in page

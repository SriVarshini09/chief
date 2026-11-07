"""The garden dashboard: real data, the same token, and no way to change anything by reading it."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from chief.dashboard import snapshot  # noqa: E402
from chief.tools.base import WRITE_TOOLS  # noqa: E402
from chief.web.app import DASHBOARD_PAGE, FOOTAGE, FOOTAGE_FILES, WORLD_SCRIPT, create_app  # noqa: E402

TOKEN = "s3cret-test-token"
H = {"X-Chief-Token": TOKEN}


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt, TOKEN))


def test_both_routes_need_the_token(client):
    for path in ("/dashboard", "/dashboard/data", "/dashboard/world.js"):
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
    assert "https://" not in page and 'src="http' not in page
    assert "http" not in world and "fetch(" not in world and "XMLHttpRequest" not in world


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


def test_every_clip_is_credited_with_its_source_and_license():
    credits = (FOOTAGE / "CREDITS.md").read_text("utf-8")
    for name in FOOTAGE_FILES:
        assert name in credits
    assert "Pixelmaniac pictures" in credits and "Bureau of Land Management" in credits
    assert credits.count("commons.wikimedia.org/wiki/File:") == 2 and "ublic domain" in credits


def test_a_device_without_webgl_keeps_the_drawn_garden():
    world = WORLD_SCRIPT.read_text("utf-8")
    page = DASHBOARD_PAGE.read_text("utf-8")
    assert "if (!gl) return null;" in world
    assert "else renderDrawnScene(t, dt);" in page

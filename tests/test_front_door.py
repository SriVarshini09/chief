"""The front door: /welcome, password sign-in, /session and the /setup page (chief/web/front_door.py)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from chief.owner import OwnerError, OwnerStore, check_password  # noqa: E402
from chief.web import front_door  # noqa: E402
from chief.web.app import create_app  # noqa: E402

TOKEN = "Xk3fP9-qLm2Zr7Vw8Tn4Hs-front"
PASSWORD = "meadow-kitten-42"
LOCAL = {"Origin": "http://localhost"}
REMOTE = {"Origin": "http://testserver"}


class FakeSTT:
    enabled = True

    def transcribe(self, audio, filename, content_type):
        return ""


class FakeComposio:
    def __init__(self, status=None, url="https://connect.composio.dev/link/abc"):
        self.status, self.url, self.links = status or {"gmail": True, "googlecalendar": False}, url, []

    def connection_status(self, toolkits=None):
        return dict(self.status)

    def connect_link(self, toolkit):
        self.links.append(toolkit)
        return self.url


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    pauses: list[float] = []

    async def fake_pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(front_door, "_pause", fake_pause)
    return pauses


@pytest.fixture
def app(rt):
    return create_app(rt, TOKEN, transcriber=FakeSTT())


@pytest.fixture
def remote(app):
    """A stranger on the internet (TestClient's default address is not loopback)."""
    return TestClient(app)


@pytest.fixture
def local(app):
    """A browser on the computer running Chief."""
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50123))


def make_owner(rt, email="me@example.com"):
    return OwnerStore(rt.cfg.state_dir).create("Sam", email, PASSWORD)


def sign_in(client, email="me@example.com", password=PASSWORD, headers=REMOTE):
    return client.post("/signin", json={"email": email, "password": password}, headers=headers)


# ---- the owner on disk ------------------------------------------------------------------------------


def test_owner_is_stored_with_an_scrypt_hash_never_the_password(rt):
    make_owner(rt)
    path = rt.cfg.state_dir / "owner.json"
    raw = path.read_text(encoding="utf-8")
    assert PASSWORD not in raw
    data = json.loads(raw)
    assert data["email"] == "me@example.com" and data["name"] == "Sam"
    assert data["password"]["algo"] == "scrypt" and len(bytes.fromhex(data["password"]["salt"])) == 16
    assert check_password(PASSWORD, data["password"]) and not check_password("meadow-kitten-43", data["password"])


def test_each_owner_gets_its_own_salt(tmp_path):
    a = OwnerStore(tmp_path / "a")
    b = OwnerStore(tmp_path / "b")
    a.create("A", "a@example.com", PASSWORD)
    b.create("B", "b@example.com", PASSWORD)
    ha, hb = (json.loads(s.path.read_text())["password"] for s in (a, b))
    assert ha["salt"] != hb["salt"] and ha["hash"] != hb["hash"]


def test_owner_rules(tmp_path):
    store = OwnerStore(tmp_path)
    with pytest.raises(OwnerError):
        store.create("Sam", "not-an-email", PASSWORD)
    with pytest.raises(OwnerError):
        store.create("Sam", "me@example.com", "short")
    with pytest.raises(OwnerError):
        store.create("", "me@example.com", PASSWORD)
    store.create("Sam", " Me@Example.com ", PASSWORD)
    with pytest.raises(OwnerError):  # one owner only
        store.create("Other", "other@example.com", PASSWORD)
    assert store.verify("me@example.com", PASSWORD).name == "Sam"
    assert store.verify("ME@example.com ", PASSWORD) is not None
    assert store.verify("me@example.com", "wrong-password!") is None
    assert store.verify("someone@example.com", PASSWORD) is None


# ---- first run: creating the account ---------------------------------------------------------------


def test_first_run_from_this_computer_creates_the_account_and_signs_in(local, rt):
    state = local.get("/signin/state").json()
    assert state == {"owner": False, "can_create": True, "signed_in": False}
    r = local.post("/signin/create", json={"name": "Sam", "email": "me@example.com", "password": PASSWORD}, headers=LOCAL)
    assert r.status_code == 200 and r.json()["next"] == "/setup"
    cookie = r.headers["set-cookie"].lower()
    assert "chief_token=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert OwnerStore(rt.cfg.state_dir).exists()
    assert local.get("/signin/state").json()["signed_in"] is True


def test_first_run_is_not_open_to_strangers(remote, rt):
    assert remote.get("/signin/state").json()["can_create"] is False
    body = {"name": "Mallory", "email": "m@example.com", "password": PASSWORD}
    r = remote.post("/signin/create", json=body, headers=REMOTE)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    assert not OwnerStore(rt.cfg.state_dir).exists()


def test_a_tunnel_from_127_0_0_1_is_not_treated_as_local(app, rt):
    tunnel = TestClient(app, base_url="https://abc.trycloudflare.com", client=("127.0.0.1", 50124))
    headers = {"Origin": "https://abc.trycloudflare.com", "Cf-Connecting-Ip": "203.0.113.9", "X-Forwarded-For": "203.0.113.9"}
    assert tunnel.get("/signin/state", headers=headers).json()["can_create"] is False
    r = tunnel.post("/signin/create", json={"name": "M", "email": "m@example.com", "password": PASSWORD}, headers=headers)
    assert r.status_code == 403 and not OwnerStore(rt.cfg.state_dir).exists()
    # a rebinding page that resolves to 127.0.0.1 still names its own host
    rebind = TestClient(app, base_url="http://evil.example", client=("127.0.0.1", 50125))
    r = rebind.post("/signin/create", json={"name": "M", "email": "m@example.com", "password": PASSWORD}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_the_private_link_lets_you_create_the_account_from_anywhere(remote, rt):
    headers = {**REMOTE, "X-Chief-Token": TOKEN}
    assert remote.get("/signin/state", headers={"X-Chief-Token": TOKEN}).json()["can_create"] is True
    r = remote.post("/signin/create", json={"name": "Sam", "email": "me@example.com", "password": PASSWORD}, headers=headers)
    assert r.status_code == 200
    again = remote.post("/signin/create", json={"name": "Other", "email": "o@example.com", "password": PASSWORD}, headers=headers)
    assert again.status_code == 409 and OwnerStore(rt.cfg.state_dir).get().name == "Sam"


def test_a_wrong_token_cannot_create_the_account(remote, rt, no_waiting):
    r = remote.post("/signin/create", json={"name": "M", "email": "m@example.com", "password": PASSWORD},
                    headers={**REMOTE, "X-Chief-Token": "wrong-token-wrong-token"})
    assert r.status_code == 401 and no_waiting and not OwnerStore(rt.cfg.state_dir).exists()


# ---- signing in ------------------------------------------------------------------------------------


def test_sign_in_sets_the_cookie_and_the_page_can_fetch_the_token(remote, rt):
    make_owner(rt)
    assert remote.get("/signin/state").json() == {"owner": True, "can_create": False, "signed_in": False}
    r = sign_in(remote)
    assert r.status_code == 200 and r.json()["next"] == "/dashboard" and r.json()["name"] == "Sam"
    assert TOKEN not in r.text  # the token travels only in the HttpOnly cookie here
    assert "httponly" in r.headers["set-cookie"].lower()
    session = remote.get("/session", headers={"X-Chief-Session": "1", "Sec-Fetch-Site": "same-origin"})
    assert session.status_code == 200 and session.json()["token"] == TOKEN
    assert session.json()["owner"] == {"name": "Sam", "email": "me@example.com"}
    assert remote.get("/dashboard/data").status_code == 200  # the cookie reads
    assert remote.post("/ask", json={"text": "hi"}).status_code == 401  # but cannot act on its own
    assert remote.post("/ask", json={"text": "how's my week looking?"}, headers={"X-Chief-Token": session.json()["token"]}).status_code == 200


def test_sign_in_only_goes_to_chief_pages(remote, rt):
    make_owner(rt)
    r = remote.post("/signin", json={"email": "me@example.com", "password": PASSWORD, "next": "https://evil.example"}, headers=REMOTE)
    assert r.json()["next"] == "/dashboard"
    r = remote.post("/signin", json={"email": "me@example.com", "password": PASSWORD, "next": "/setup"}, headers=REMOTE)
    assert r.json()["next"] == "/setup"


def test_wrong_password_and_unknown_email_get_the_same_generic_answer(remote, rt):
    make_owner(rt)
    wrong_pw = sign_in(remote, password="not-my-password")
    wrong_email = sign_in(remote, email="nobody@example.com")
    assert wrong_pw.status_code == wrong_email.status_code == 401
    assert wrong_pw.json() == wrong_email.json() == {"detail": front_door.GENERIC_WRONG}
    assert "set-cookie" not in wrong_pw.headers


def test_sign_in_before_any_account_exists_is_refused(remote):
    assert sign_in(remote).status_code == 401


def test_wrong_passwords_slow_down_then_lock_out(remote, rt, no_waiting):
    make_owner(rt)
    for _ in range(3):
        sign_in(remote, password="guess-guess-guess")
    assert no_waiting == [0.5, 1.0, 2.0]  # each wrong try waits longer
    for _ in range(front_door.MAX_WRONG - 3):
        sign_in(remote, password="guess-guess-guess")
    assert max(no_waiting) == front_door.MAX_DELAY
    locked = sign_in(remote)  # even the right password waits out the window
    assert locked.status_code == 429 and "set-cookie" not in locked.headers


def test_a_good_sign_in_clears_the_count(remote, rt, no_waiting):
    make_owner(rt)
    sign_in(remote, password="guess-guess-guess")
    sign_in(remote, password="guess-guess-guess")
    assert sign_in(remote).status_code == 200
    no_waiting.clear()
    sign_in(remote, password="guess-guess-guess")
    assert no_waiting == [0.5]


# ---- cross-site requests ---------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Referer": "https://evil.example/page"},
    {},  # no Origin and no Referer: refused too
    {"Origin": "null"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "same-site"},
])
def test_cross_site_login_and_sign_up_are_refused(remote, rt, headers):
    body = {"name": "M", "email": "me@example.com", "password": PASSWORD}
    assert remote.post("/signin/create", json=body, headers={**headers, "X-Chief-Token": TOKEN}).status_code == 403
    make_owner(rt)
    r = remote.post("/signin", json=body, headers=headers)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    assert remote.post("/signout", json={}, headers=headers).status_code == 403


def test_same_origin_referer_is_accepted_without_origin(remote, rt):
    make_owner(rt)
    assert sign_in(remote, headers={"Referer": "http://testserver/signin"}).status_code == 200


def test_an_html_form_post_is_refused(remote, rt):
    make_owner(rt)
    r = remote.post("/signin", data={"email": "me@example.com", "password": PASSWORD}, headers=REMOTE)
    assert r.status_code == 415


def test_session_is_for_chief_pages_only(remote, rt):
    assert remote.get("/session", headers={"X-Chief-Session": "1"}).status_code == 401  # not signed in
    make_owner(rt)
    sign_in(remote)
    assert remote.get("/session").status_code == 403  # no custom header: a plain link or <img>
    assert remote.get("/session", headers={"X-Chief-Session": "1", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert remote.get("/session", headers={"X-Chief-Session": "1", "Origin": "https://evil.example"}).status_code == 403
    r = remote.get("/session", headers={"X-Chief-Session": "1"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"


def test_sign_out_clears_the_cookie(remote, rt):
    make_owner(rt)
    sign_in(remote)
    assert remote.get("/dashboard/data").status_code == 200
    r = remote.post("/signout", json={}, headers=REMOTE)
    assert r.status_code == 200 and r.json()["next"] == "/welcome"
    assert 'chief_token=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    assert remote.get("/dashboard/data").status_code == 401


# ---- public pages ----------------------------------------------------------------------------------


def test_welcome_is_public_and_holds_no_data(remote, rt):
    make_owner(rt)
    r = remote.get("/welcome")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Get started" in r.text and "Sign in" in r.text and "asks you before anything changes" in r.text
    assert TOKEN not in r.text and "me@example.com" not in r.text and "Sam" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "set-cookie" not in r.headers
    assert remote.get("/signin").status_code == 200 and remote.get("/front.css").status_code == 200
    state = remote.get("/signin/state").text
    assert "me@example.com" not in state and "Sam" not in state


def test_signed_in_pages_send_a_browser_to_sign_in(remote):
    for path in ("/dashboard", "/setup"):
        r = remote.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/signin?next={path}"
        assert remote.get(path).status_code == 401  # scripts and tools still get a plain 401
    assert remote.get("/", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 401  # the phone page is unchanged


def test_existing_token_links_still_work(remote):
    for path in ("/", "/dashboard", "/stage", "/setup", "/status", "/dashboard/data"):
        r = remote.get(path, params={"token": TOKEN})
        assert r.status_code == 200, path
    assert "chief_token=" in remote.get("/dashboard", params={"token": TOKEN}).headers["set-cookie"]
    manifest = remote.get("/manifest.webmanifest", params={"token": TOKEN}).json()
    assert manifest["start_url"].startswith("/?token=")


def test_dashboard_links_setup_and_sign_out(remote):
    page = remote.get("/dashboard", params={"token": TOKEN}).text
    assert 'id="setup" href="/setup"' in page and 'id="signOut"' in page and '"/signout"' in page and "X-Chief-Session" in page


# ---- the setup page --------------------------------------------------------------------------------


def test_setup_status_shows_key_names_never_values(remote, rt, monkeypatch):
    secret = "sk-live-VALUE-that-must-never-show-9f8e7d"
    monkeypatch.setenv("COMPOSIO_API_KEY", secret)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:telegram-secret-value")
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    r = remote.get("/setup/status", headers={"X-Chief-Token": TOKEN})
    assert r.status_code == 200
    assert secret not in r.text and "telegram-secret-value" not in r.text and TOKEN not in r.text
    keys = {k["name"]: k["set"] for k in r.json()["keys"]}
    assert keys["COMPOSIO_API_KEY"] is True and keys["PARALLEL_API_KEY"] is False
    assert set(r.json()["keys"][0]) == {"name", "why", "set"}


def test_setup_in_mock_mode_shows_sample_data(remote):
    s = remote.get("/setup/status", headers={"X-Chief-Token": TOKEN}).json()
    assert s["mode"] == "mock" and s["google"]["state"] == "sample"
    assert s["telegram"]["doc"].endswith("docs/telegram-setup.md")
    assert s["location"] == {"shared": False} and s["brief"]["hour"] == 8
    r = remote.post("/setup/connect/gmail", headers={"X-Chief-Token": TOKEN})
    assert r.status_code == 409 and "sample data" in r.json()["detail"]


def test_setup_live_status_and_connect_link(app, rt, monkeypatch):
    rt.cfg.mode = "live"
    monkeypatch.setenv("COMPOSIO_API_KEY", "sk-test-not-real")
    fake = FakeComposio()
    app.state.composio_factory = lambda: fake
    client = TestClient(app)
    h = {"X-Chief-Token": TOKEN}
    g = client.get("/setup/status", headers=h).json()["google"]
    assert g["state"] == "not_connected" and g["toolkits"] == {"gmail": True, "googlecalendar": False}
    r = client.post("/setup/connect/calendar", headers=h)
    assert r.status_code == 200 and r.json() == {"url": fake.url} and fake.links == ["calendar"]
    fake.status = {"gmail": True, "googlecalendar": True}
    assert client.get("/setup/status?fresh=1", headers=h).json()["google"]["state"] == "connected"
    assert client.post("/setup/connect/slack", headers=h).status_code == 404
    assert client.post("/setup/connect/gmail").status_code == 401  # the cookie alone cannot make links
    fake.url = "javascript:alert(1)"
    assert client.post("/setup/connect/gmail", headers=h).status_code == 502


def test_setup_live_without_a_key_and_with_composio_down(app, rt, monkeypatch):
    rt.cfg.mode = "live"
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    client = TestClient(app)
    h = {"X-Chief-Token": TOKEN}
    assert client.get("/setup/status", headers=h).json()["google"]["state"] == "no_key"
    monkeypatch.setenv("COMPOSIO_API_KEY", "sk-test-not-real")

    class Down:
        def connection_status(self):
            raise RuntimeError("bad key sk-test-not-real")

    app.state.composio_factory = Down
    g = client.get("/setup/status?fresh=1", headers=h).json()["google"]
    assert g == {"state": "error", "mode": "live", "toolkits": {}, "detail": "RuntimeError"}


def test_startup_message_prints_the_welcome_link(monkeypatch, capsys, rt):
    from chief.__main__ import _serve_web

    monkeypatch.setenv("CHIEF_WEB_TOKEN", "Xk3fP9-qLm2Zr7Vw8Tn4Hs")
    monkeypatch.setattr("chief.web.app.serve", lambda *a: None)
    monkeypatch.setattr("chief.telegram_app.start_with_web", lambda rt: False)
    assert _serve_web(rt, "127.0.0.1", 8782) == 0
    assert "http://localhost:8782/welcome" in capsys.readouterr().out

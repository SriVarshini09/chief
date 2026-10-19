"""The real-window layer: event -> URL mapping, throttling, http(s)-only, the dedicated profile. No browser needed."""

import base64
import hashlib
import json
import socket
import struct
import threading
from datetime import date
from pathlib import Path

import pytest

from chief import screen
from chief.activity import Activity, ActivityBus
from chief.config import Config
from chief.screen import ChromeLauncher, DryRunLauncher, ScreenLayer, Target

TODAY = date(2026, 10, 19)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ev(kind, tool=None, detail=None, ok=True, seq=1):
    return Activity(seq, "2026-10-19T09:00:00", kind, "t", tool, detail or {}, ok)


def make(**kw):
    launcher = kw.pop("launcher", None) or DryRunLauncher()
    clock = kw.pop("clock", None) or Clock()
    kw.setdefault("refresh_delay", 0)  # reload at once in tests; the delay has its own test
    return ScreenLayer(launcher, today=lambda: TODAY, clock=clock, **kw), launcher, clock


def urls(launcher):
    return [t.url for t in launcher.opened]


# ---- mapping


def test_email_reads_open_the_gmail_inbox():
    layer, launcher, _ = make()
    layer(ev("read", "fetch_emails", {"result": []}))
    assert urls(launcher) == ["https://mail.google.com/mail/u/0/#inbox"]
    layer(ev("read", "fetch_thread"))  # same page: de-duplicated, not opened twice
    assert len(launcher.opened) == 1


def test_calendar_reads_open_the_day_that_was_asked_about():
    layer, launcher, _ = make()
    layer(ev("read", "list_events", {"args": {"start": "2026-11-03T00:00:00", "end": "2026-11-04T00:00:00"}, "result": []}))
    assert urls(launcher) == ["https://calendar.google.com/calendar/u/0/r/day/2026/11/3"]  # no zero padding


def test_calendar_day_falls_back_to_result_then_today():
    layer, launcher, _ = make()
    layer(ev("read", "find_free_slots", {"args": {}, "result": [["2026-12-24T10:00:00", "2026-12-24T11:00:00"]]}))
    layer2, launcher2, _ = make()
    layer2(ev("read", "list_events", {"args": {}, "result": []}))
    assert urls(launcher) == ["https://calendar.google.com/calendar/u/0/r/day/2026/12/24"]
    assert urls(launcher2) == ["https://calendar.google.com/calendar/u/0/r/day/2026/10/19"]


@pytest.mark.parametrize(
    "tool,detail",
    [
        ("create_event", {"args": {"title": "x", "start": "2026-10-21T15:00:00-07:00", "end": "2026-10-21T16:00:00-07:00"}}),
        ("patch_event", {"args": {"event_id": "e1", "fields": {"start": {"dateTime": "2026-10-21T15:00:00Z"}}}}),
    ],
)
def test_calendar_writes_show_the_day_they_touch(tool, detail):
    layer, launcher, _ = make()
    layer(ev("write", tool, detail))
    assert urls(launcher) == ["https://calendar.google.com/calendar/u/0/r/day/2026/10/21"]


def test_delete_event_reuses_the_day_last_shown():
    layer, launcher, clock = make()
    layer(ev("read", "list_events", {"args": {"start": "2026-10-22T00:00:00"}}))
    clock.t += 60  # past the de-duplication window, so the same page is opened again
    layer(ev("write", "delete_event", {"args": {"event_id": "e1"}}))
    assert urls(launcher) == ["https://calendar.google.com/calendar/u/0/r/day/2026/10/22"] * 2


def test_drafts_and_sent_mail_open_the_matching_gmail_view():
    layer, launcher, _ = make()
    layer(ev("write", "create_draft", {"args": {"to": "a@b.c"}}))
    layer(ev("write", "send_email", {"args": {"to": "a@b.c"}}))
    assert urls(launcher) == ["https://mail.google.com/mail/u/0/#drafts", "https://mail.google.com/mail/u/0/#sent"]


def test_other_events_and_failures_open_nothing():
    layer, launcher, _ = make()
    for e in (
        ev("heard"), ev("say"), ev("proposal"), ev("approved"), ev("working", "fetch_emails"), ev("read", "read_health"),
        ev("write", "post_slack"), ev("error", "list_events", ok=False), ev("read", "fetch_emails", ok=False),
    ):
        layer(e)
    assert launcher.opened == []


# ---- web search: real result pages, http(s) only


def test_search_opens_the_top_result_urls_in_one_window():
    layer, launcher, _ = make()
    results = [{"title": str(i), "url": f"https://example.org/p{i}"} for i in range(5)]
    layer(ev("read", "search_web", {"result": results}))
    assert urls(launcher) == [f"https://example.org/p{i}" for i in range(3)]  # capped at the top three
    assert [t.new_window for t in launcher.opened] == [True, False, False]  # extra pages join the first window


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)", "file:///C:/Windows/win.ini", "data:text/html,hi", "ftp://example.org/x", "chrome://settings",
        "about:blank", "//example.org/x", "example.org/x", "https://", "https://user:pw@example.org/", "https://exa mple.org/",
        "https://example.org/\nx", "", None, 42, "https://example.org/" + "a" * 3000, "https://example.org/" + "a" * 381 + "...",
    ],
)
def test_non_http_urls_are_never_opened(bad):
    assert screen.http_url(bad) is None
    layer, launcher, _ = make()
    layer(ev("read", "search_web", {"result": [{"url": bad}, {"title": "no url"}, "junk"]}))
    assert launcher.opened == []


def test_search_skips_bad_urls_and_keeps_the_good_ones():
    layer, launcher, _ = make()
    layer(ev("read", "search_web", {"result": [{"url": "javascript:x"}, {"url": "http://a.example/"}, {"url": "http://a.example"}, {"url": "https://b.example/x"}]}))
    assert urls(launcher) == ["http://a.example/", "https://b.example/x"]  # also de-duplicated within the result


# ---- throttle and de-duplication


def test_same_url_is_not_opened_twice_within_the_window_but_is_after_it():
    layer, launcher, clock = make(dedupe_seconds=30)
    e = ev("read", "fetch_emails")
    layer(e)
    clock.t += 10
    layer(e)
    assert len(launcher.opened) == 1
    clock.t += 31
    layer(e)
    assert len(launcher.opened) == 2


def test_windows_per_request_are_capped_and_a_new_request_resets_the_budget():
    layer, launcher, clock = make(max_per_request=2, max_search_urls=10)
    layer(ev("read", "search_web", {"result": [{"url": f"https://example.org/{i}"} for i in range(6)]}))
    assert len(launcher.opened) == 2
    layer(ev("heard"))  # the next thing you say is a new request
    layer(ev("read", "search_web", {"result": [{"url": f"https://example.net/{i}"} for i in range(6)]}))
    assert len(launcher.opened) == 4


def test_a_long_quiet_gap_also_starts_a_new_request():
    layer, launcher, clock = make(max_per_request=1, request_gap=90)
    layer(ev("read", "fetch_emails"))
    layer(ev("read", "list_events", {"args": {"start": "2026-10-20T00:00:00"}}))
    assert len(launcher.opened) == 1
    clock.t += 120
    layer(ev("read", "list_events", {"args": {"start": "2026-10-20T00:00:00"}}))
    assert len(launcher.opened) == 2


def test_after_a_write_the_calendar_is_reloaded_in_place_when_the_launcher_can():
    launcher = DryRunLauncher(can_refresh=True)
    layer, _, clock = make(launcher=launcher)
    day = {"args": {"start": "2026-10-19T00:00:00"}}
    layer(ev("read", "list_events", day))
    clock.t += 5  # well inside the 30 s de-dup window; a refresh is not a duplicate window
    layer(ev("write", "patch_event", {"args": {"event_id": "e", "fields": {"start": "2026-10-19T10:00:00"}}}))
    assert [t.refresh for t in launcher.opened] == [False, True]
    layer(ev("write", "patch_event", {"args": {"event_id": "e", "fields": {"start": "2026-10-19T11:00:00"}}}))
    assert len(launcher.opened) == 2  # but refreshes are rate limited too


def test_without_in_place_reload_a_write_does_not_pile_up_duplicate_windows():
    layer, launcher, clock = make()  # can_refresh False
    layer(ev("read", "list_events", {"args": {"start": "2026-10-19T00:00:00"}}))
    clock.t += 5
    layer(ev("write", "patch_event", {"args": {"event_id": "e", "fields": {"start": "2026-10-19T10:00:00"}}}))
    assert len(launcher.opened) == 1


def test_a_broken_launcher_never_breaks_chief_and_is_retried_later():
    class Boom:
        can_refresh = False

        def open(self, t):
            raise OSError("no display")

    layer, _, _ = make(launcher=Boom())
    bus = ActivityBus()
    bus.subscribe(layer)
    bus.emit("read", "Read your email", tool="fetch_emails")  # must not raise
    assert layer._seen == {}


def test_works_as_a_bus_subscriber():
    layer, launcher, _ = make()
    bus = ActivityBus()
    bus.subscribe(layer)
    bus.emit("heard", "what is on today")
    bus.emit("read", "Read your calendar", tool="list_events", detail={"args": {"start": "2026-10-19T00:00:00"}, "result": []})
    assert urls(launcher) == ["https://calendar.google.com/calendar/u/0/r/day/2026/10/19"]


# ---- the dedicated browser profile


def test_browser_args_use_the_dedicated_profile_and_window_options(tmp_path):
    profile = tmp_path / "browser-profile"
    args = screen.browser_args(
        "chrome.exe", profile, "https://mail.google.com/mail/u/0/#inbox",
        size=(1280, 800), pos=(1920, 0), debug_port=9222,
    )
    assert args[0] == "chrome.exe"
    assert f"--user-data-dir={profile}" in args
    assert "--new-window" in args and "--window-size=1280,800" in args and "--window-position=1920,0" in args
    assert "--remote-debugging-port=9222" in args
    assert args[-1] == "https://mail.google.com/mail/u/0/#inbox"
    assert "--new-window" not in screen.browser_args("c", profile, "https://x.test/", new_window=False)


def test_launcher_spawns_with_the_profile_and_never_without_one(tmp_path):
    calls = []
    profile = tmp_path / "p"
    launcher = ChromeLauncher("chrome.exe", profile, size=(100, 200), popen=lambda args, **kw: calls.append(args))
    assert launcher.open(Target("https://mail.google.com/mail/u/0/#inbox", "gmail"))
    assert launcher.login()
    assert profile.is_dir()
    assert all(f"--user-data-dir={profile}" in c for c in calls) and len(calls) == 2
    assert not any(a.startswith("--remote-debugging-port") for a in calls[1])  # sign-in runs without DevTools
    assert launcher.open(Target("javascript:alert(1)", "web")) is False and len(calls) == 2  # refused at the last gate


def test_window_option_parsing():
    assert screen.parse_size("1280x800") == (1280, 800)
    assert screen.parse_pos("1920,0") == (1920, 0)
    assert screen.parse_pos("-1920,0") == (-1920, 0)  # a monitor to the left
    assert screen.parse_size("big") is None and screen.parse_pos(None) is None


def test_find_browser_prefers_explicit_then_chrome_then_edge():
    env = {"PROGRAMFILES": "P", "PROGRAMFILES(X86)": "Q", "LOCALAPPDATA": "L"}
    chrome = str(Path("P") / "Google" / "Chrome" / "Application" / "chrome.exe")
    edge = str(Path("Q") / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    none = lambda n: None
    assert screen.find_browser(None, env, lambda p: p in (chrome, edge), none, "win32") == chrome
    assert screen.find_browser(None, env, lambda p: p == edge, none, "win32") == edge
    assert screen.find_browser("X:/mine.exe", env, lambda p: p == "X:/mine.exe", none, "win32") == "X:/mine.exe"
    with pytest.raises(screen.BrowserNotFound):
        screen.find_browser(None, env, lambda p: False, none, "win32")
    with pytest.raises(screen.BrowserNotFound):
        screen.find_browser("X:/missing.exe", env, lambda p: False, none, "win32")


def test_config_reads_the_show_env_and_defaults_to_off(monkeypatch, tmp_path):
    for k in ("CHIEF_SHOW_WINDOWS", "CHIEF_SHOW_PROFILE_DIR", "CHIEF_SHOW_DEBUG_PORT", "CHIEF_SHOW_DRY_RUN", "CHIEF_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env()
    assert cfg.show_windows is False and cfg.show_dry_run is False and cfg.show_debug_port is None
    assert cfg.show_profile_dir == cfg.state_dir / "browser-profile"
    monkeypatch.setenv("CHIEF_SHOW_WINDOWS", "1")
    monkeypatch.setenv("CHIEF_SHOW_PROFILE_DIR", str(tmp_path / "prof"))
    monkeypatch.setenv("CHIEF_SHOW_DEBUG_PORT", "9222")
    cfg = Config.from_env()
    assert cfg.show_windows is True and cfg.show_profile_dir == tmp_path / "prof" and cfg.show_debug_port == 9222


# ---- CLI wiring and the demo (dry run: no browser)


def test_demo_replays_through_the_layer_without_a_browser(capsys, monkeypatch):
    cfg = Config()
    cfg.show_dry_run = True
    assert screen.show_demo(cfg, sleep=lambda s: None, interactive=False) == 0
    out = capsys.readouterr().out
    assert "TEST Google account" in out and "changes nothing" in out
    assert "mail.google.com" in out and "calendar.google.com" in out and "caltrain.com" in out
    assert not [l for l in out.splitlines() if l.startswith("[screen]") and "javascript" in l]  # the bad url was filtered


def test_cli_flags_and_subcommands(monkeypatch, capsys):
    from chief import __main__ as cli

    monkeypatch.setenv("CHIEF_SHOW_DRY_RUN", "1")
    monkeypatch.setenv("CHIEF_STATE_DIR", str(Path(__file__).parent / "_unused_state"))
    assert cli.main(["show-login"]) == 0
    assert "TEST Google account" in capsys.readouterr().out
    attached = []
    monkeypatch.setattr(screen, "attach", lambda rt, cfg=None: attached.append(rt))
    monkeypatch.delenv("CHIEF_SHOW_WINDOWS", raising=False)
    cli.main(["run", "--once", "--now", "2026-10-19T03:00:00"])  # outside active hours: nothing to do
    assert attached == []
    cli.main(["run", "--once", "--show-windows", "--now", "2026-10-19T03:00:00"])
    assert len(attached) == 1


# ---- optional DevTools mode: stdlib websocket client against a fake DevTools server


def test_ws_frame_is_masked_and_round_trips():
    frame = screen.ws_frame(b"hello", mask_key=b"\x01\x02\x03\x04")
    assert frame[0] == 0x81 and frame[1] == 0x80 | 5 and frame[2:6] == b"\x01\x02\x03\x04"
    assert bytes(b ^ b"\x01\x02\x03\x04"[i % 4] for i, b in enumerate(frame[6:])) == b"hello"
    big = screen.ws_frame(b"x" * 300)
    assert big[1] == 0x80 | 126 and struct.unpack(">H", big[2:4])[0] == 300


def _fake_devtools():
    """A one-shot DevTools endpoint: answers the websocket upgrade, records the command, replies with id 1."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    got = {}

    def serve():
        conn, _ = server.accept()
        f = conn.makefile("rb")
        key = ""
        while (line := f.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"sec-websocket-key"):
                key = line.split(b":", 1)[1].strip().decode()
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        h = f.read(2)
        assert h[1] & 0x80, "client frames must be masked"
        n = h[1] & 0x7F
        mask = f.read(4)
        got["msg"] = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(f.read(n))))
        noise = json.dumps({"method": "Page.frameNavigated"}).encode()  # an event before the reply must be skipped
        reply = json.dumps({"id": 1, "result": {"ok": True}}).encode()
        conn.sendall(b"\x81" + bytes([len(noise)]) + noise + b"\x81" + bytes([len(reply)]) + reply)
        conn.close()
        server.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return server.getsockname()[1], got, t


def test_cdp_call_speaks_websocket_to_a_local_endpoint():
    port, got, t = _fake_devtools()
    result = screen.cdp_call(f"ws://127.0.0.1:{port}/devtools/page/1", "Page.reload", {"ignoreCache": True})
    t.join(2)
    assert result == {"ok": True}
    assert got["msg"] == {"id": 1, "method": "Page.reload", "params": {"ignoreCache": True}}


def test_cdp_refuses_non_local_endpoints():
    with pytest.raises(ValueError):
        screen.cdp_call("ws://example.com:9222/devtools/page/1", "Page.reload")


# ---- safety: no local or internal targets, no replayed data, never the real browser profile, off unless asked


@pytest.mark.parametrize(
    "internal",
    [
        "http://localhost/", "http://LOCALHOST:8080/x", "http://app.localhost/", "http://127.0.0.1/", "http://127.9.9.9:80/",
        "http://10.0.0.5/", "http://172.16.0.1/", "http://172.31.255.254/", "http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/", "http://[::1]/", "http://[fe80::1]/", "http://[fd00::1]/", "http://[::ffff:127.0.0.1]/",
        "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://127.1/", "http://100.64.0.1/",
        "http://router/", "http://printer.local/", "http://wiki.internal/", "http://localhost./",
    ],
)
def test_local_and_internal_targets_are_never_opened(internal):
    assert screen.http_url(internal) is None
    layer, launcher, _ = make()
    layer(ev("read", "search_web", {"result": [{"url": internal}]}))
    assert launcher.opened == []


@pytest.mark.parametrize("public", ["https://example.org/", "http://93.184.216.34/", "https://face.be/", "https://172.32.0.1/", "https://[2606:4700::1111]/"])
def test_ordinary_public_urls_still_open(public):
    assert screen.http_url(public) == public


def test_cached_and_simulated_events_open_nothing():
    layer, launcher, _ = make()
    layer(ev("read", "fetch_emails", {"cached": True}))
    layer(ev("read", "list_events", {"simulated": True, "args": {"start": "2026-10-19T00:00:00"}}))
    layer(ev("read", "search_web", {"cached": True, "result": [{"url": "https://example.org/"}]}))
    layer(ev("write", "patch_event", {"simulated": True}))
    assert launcher.opened == []
    layer(ev("read", "fetch_emails", {"cached": False}))  # a live event right after still works
    assert len(launcher.opened) == 1


def test_profile_dir_is_never_the_real_browser_profile(tmp_path):
    home = tmp_path / "home"
    env = {"LOCALAPPDATA": str(home / "AppData" / "Local")}
    chrome = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    edge = home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
    for bad in (chrome, edge, chrome / "Default", chrome.parent, home / "AppData" / "Local"):
        with pytest.raises(screen.UnsafeProfile):
            screen.check_profile_dir(bad, env, home, "win32")
    ok = screen.check_profile_dir(tmp_path / ".chief" / "browser-profile", env, home, "win32")
    assert ok == (tmp_path / ".chief" / "browser-profile").resolve()
    with pytest.raises(screen.UnsafeProfile):
        screen.check_profile_dir(home / ".config" / "google-chrome", env, home, "linux")
    with pytest.raises(screen.UnsafeProfile):
        screen.check_profile_dir(home / "Library" / "Application Support" / "Google" / "Chrome", env, home, "darwin")


def test_build_launcher_refuses_the_default_profile(monkeypatch, capsys):
    monkeypatch.setattr(screen, "find_browser", lambda explicit=None, *a, **k: "chrome.exe")
    cfg = Config()
    cfg.show_profile_dir = screen.default_profile_dirs()[0]
    with pytest.raises(screen.UnsafeProfile):
        screen.build_launcher(cfg)
    assert screen.show_login(cfg) == 1  # and the helper says so instead of opening it
    assert "normal browser profile" in capsys.readouterr().out


class _NoBrowser:
    """Records every attempt to start a browser."""

    def __init__(self):
        self.started = []

    def __call__(self, args, **kw):
        self.started.append(args)


def _cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEF_MODE", "mock")
    monkeypatch.setenv("CHIEF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEF_SHOW_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(screen, "find_browser", lambda explicit=None, *a, **k: "chrome.exe")
    for k in ("CHIEF_SHOW_WINDOWS", "CHIEF_SHOW_DEBUG_PORT", "CHIEF_SHOW_DRY_RUN", "CHIEF_SHOW_WINDOW_POS", "CHIEF_SHOW_WINDOW_SIZE"):
        monkeypatch.delenv(k, raising=False)


def test_nothing_opens_unless_windows_are_switched_on(monkeypatch, tmp_path):
    import subprocess

    from chief import __main__ as cli

    popen = _NoBrowser()
    monkeypatch.setattr(subprocess, "Popen", popen)
    _cli_env(monkeypatch, tmp_path)
    cli.main(["run", "--once", "--now", "2026-10-21T08:00:00"])
    assert popen.started == []  # a full mock heartbeat read mail and calendar, and no window appeared
    monkeypatch.setenv("CHIEF_SHOW_WINDOWS", "0")
    cli.main(["run", "--once", "--now", "2026-10-21T08:00:00"])
    assert popen.started == []


def test_show_windows_flag_wires_the_layer_to_a_real_run(monkeypatch, tmp_path):
    import subprocess

    from chief import __main__ as cli

    popen = _NoBrowser()
    monkeypatch.setattr(subprocess, "Popen", popen)
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CHIEF_SHOW_WINDOW_POS", "1920,0")
    monkeypatch.setenv("CHIEF_SHOW_WINDOW_SIZE", "1280x800")
    cli.main(["run", "--once", "--show-windows", "--now", "2026-10-21T08:00:00"])
    assert popen.started, "the flag should open at least the Gmail or Calendar window"
    for args in popen.started:
        assert f"--user-data-dir={(tmp_path / 'profile').resolve()}" in args
        assert "--window-position=1920,0" in args and "--window-size=1280,800" in args
        assert args[-1].startswith(("https://mail.google.com/", "https://calendar.google.com/"))


# ---- DevTools mode: one Gmail tab, one Calendar tab, reloaded in place after a write; fall back once if unreachable


def _fake_cdp(monkeypatch, tabs):
    calls = {"cdp": [], "http": []}

    def http(port, path, method="GET", timeout=1.5):
        calls["http"].append((method, path))
        if path == "/json/list":
            return tabs
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
        return ""

    monkeypatch.setattr(screen, "_cdp_http", http)
    monkeypatch.setattr(screen, "cdp_call", lambda ws, method, params=None, timeout=2.0: calls["cdp"].append((ws, method, params)) or {})
    return calls


def _tab(url, ident="t1"):
    return {"type": "page", "id": ident, "url": url, "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{ident}"}


CAL = "https://calendar.google.com/calendar/u/0/r/day/2026/10/19"
INBOX = "https://mail.google.com/mail/u/0/#inbox"


def test_devtools_reloads_the_one_calendar_tab_after_a_write(monkeypatch):
    calls = _fake_cdp(monkeypatch, [_tab(CAL), _tab(INBOX, "t2")])
    assert screen._cdp_show(9222, Target(CAL, "calendar", refresh=True), None, None)
    assert [c[1] for c in calls["cdp"]] == ["Page.reload"]
    assert ("GET", "/json/activate/t1") in calls["http"]  # brought to the front; nothing created, nothing clicked


def test_devtools_keeps_one_tab_per_app(monkeypatch):
    calls = _fake_cdp(monkeypatch, [_tab(CAL)])
    other = "https://calendar.google.com/calendar/u/0/r/day/2026/10/20"
    screen._cdp_show(9222, Target(other, "calendar"), None, None)  # another day: navigate the same tab
    assert calls["cdp"] == [("ws://127.0.0.1:9222/devtools/page/t1", "Page.navigate", {"url": other})]
    calls["cdp"].clear()
    screen._cdp_show(9222, Target(CAL, "calendar"), None, None)  # the page already shown: just bring it forward
    assert calls["cdp"] == [] and ("GET", "/json/activate/t1") in calls["http"]
    assert not any(c[1] == "Target.createTarget" for c in calls["cdp"])


def test_devtools_creates_a_window_with_the_configured_placement_when_no_tab_exists(monkeypatch):
    calls = _fake_cdp(monkeypatch, [])
    screen._cdp_show(9222, Target(CAL, "calendar"), (1280, 800), (1920, 0))
    ws, method, params = calls["cdp"][0]
    assert method == "Target.createTarget" and ws.endswith("/devtools/browser/x")
    assert params == {"url": CAL, "newWindow": True, "width": 1280, "height": 800, "left": 1920, "top": 0}
    calls["cdp"].clear()
    screen._cdp_show(9222, Target("https://example.org/", "web", new_window=False), (1280, 800), (1920, 0))
    assert calls["cdp"][0][2]["newWindow"] is False  # later pages join that window as tabs


def test_delayed_reload_is_scheduled_after_the_write():
    launcher = DryRunLauncher(can_refresh=True)
    pending = []
    layer, _, clock = make(launcher=launcher, refresh_delay=1.5, schedule=lambda d, fn: pending.append((d, fn)))
    layer(ev("read", "list_events", {"args": {"start": "2026-10-19T00:00:00"}}))
    clock.t += 5
    layer(ev("write", "patch_event", {"args": {"event_id": "e", "fields": {"start": "2026-10-19T10:00:00"}}}))
    assert [t.refresh for t in launcher.opened] == [False]  # not yet: the write has only just happened
    assert [d for d, _ in pending] == [1.5]
    pending[0][1]()
    assert [t.refresh for t in launcher.opened] == [False, True]


def test_real_timer_fires_and_flush_waits_for_it():
    launcher = DryRunLauncher(can_refresh=True)
    layer = ScreenLayer(launcher, today=lambda: TODAY, refresh_delay=0.05)
    layer(ev("write", "patch_event", {"args": {"event_id": "e", "fields": {"start": "2026-10-19T10:00:00"}}}))
    layer.flush(2)
    assert [t.refresh for t in launcher.opened] == [True]


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_unreachable_devtools_port_falls_back_to_plain_windows_and_says_so_once(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(screen, "time", _FakeTime())
    monkeypatch.setattr(screen, "_cdp_alive", lambda port: False)
    launcher = ChromeLauncher("chrome.exe", tmp_path / "p", debug_port=9222, popen=lambda a, **k: calls.append(a))
    assert launcher.can_refresh is False
    assert launcher.open(Target(CAL, "calendar"))  # first launch starts the browser with the port
    assert launcher.open(Target(INBOX, "gmail"))  # the port never came up: a plain window
    assert launcher.open(Target("https://example.org/", "web"))
    out = capsys.readouterr().out
    assert out.count("DevTools port 9222 is not reachable") == 1
    assert len(calls) == 3 and all("--remote-debugging-port=9222" in c for c in calls)


def test_devtools_port_coming_up_after_launch_is_used(monkeypatch, tmp_path):
    spawned, shown = [], []
    fake = _FakeTime()
    monkeypatch.setattr(screen, "time", fake)
    monkeypatch.setattr(screen, "_cdp_alive", lambda port: fake.now >= 1.0 and bool(spawned))
    monkeypatch.setattr(screen, "_cdp_show", lambda port, t, size, pos: shown.append(t.url) or True)
    launcher = ChromeLauncher("chrome.exe", tmp_path / "p", debug_port=9222, popen=lambda a, **k: spawned.append(a))
    launcher.open(Target(CAL, "calendar"))  # starts the browser
    launcher.open(Target(INBOX, "gmail"))  # waits for the port, then reuses DevTools
    assert len(spawned) == 1 and shown == [INBOX]


# ---- window placement helper


def test_layout_suggests_the_secondary_monitor():
    mons = [screen.Monitor(0, 0, 1920, 1080, True), screen.Monitor(1920, 0, 2560, 1440)]
    text = "\n".join(screen.suggest_layout(mons))
    assert "CHIEF_SHOW_WINDOW_POS=1920,0" in text and "CHIEF_SHOW_WINDOW_SIZE=2560x1440" in text
    left = "\n".join(screen.suggest_layout([screen.Monitor(0, 0, 1920, 1080, True), screen.Monitor(-1280, 0, 1280, 1024)]))
    assert "CHIEF_SHOW_WINDOW_POS=-1280,0" in left


def test_layout_with_one_monitor_suggests_the_right_half(capsys):
    text = "\n".join(screen.suggest_layout([screen.Monitor(0, 0, 1920, 1080, True)]))
    assert "CHIEF_SHOW_WINDOW_POS=960,0" in text and "960x1080" in text
    assert "No monitors detected" in screen.suggest_layout([])[0]
    assert screen.show_layout(Config(), monitors=[screen.Monitor(0, 0, 800, 600, True)]) == 0
    assert "Monitors" in capsys.readouterr().out


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows only")
def test_detect_monitors_finds_a_primary_on_this_machine():
    mons = screen.detect_monitors()
    assert mons and mons[0].primary and mons[0].w > 0 and mons[0].h > 0


def test_show_layout_subcommand(capsys, monkeypatch):
    from chief import __main__ as cli

    monkeypatch.setattr(screen, "detect_monitors", lambda: [screen.Monitor(0, 0, 1000, 700, True)])
    assert cli.main(["show-layout"]) == 0
    assert "Monitors" in capsys.readouterr().out

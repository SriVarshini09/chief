"""The real-window layer: open (and refresh) the actual apps while Chief works, for the live stage demo.

This is a scripted PRESENTATION layer on top of the activity stream (`rt.bus`). The real work still happens through
Composio and Parallel; this module only decides which URL is worth showing and hands it to a browser. It never
clicks, types, sends or changes anything in those apps: the only things it does are start a browser on a URL and,
when the optional DevTools mode is on, navigate or reload a tab it already shows. Real changes to Gmail or Calendar
happen only through the approval gate (chief/gate.py); this layer merely reacts to the event that follows one.

It runs a DEDICATED browser profile (CHIEF_SHOW_PROFILE_DIR) so only the test Google account is ever visible.
Standard library only. See docs/screen-setup.md.
"""

from __future__ import annotations

import atexit
import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from chief.activity import Activity, ActivityBus

GMAIL_INBOX = "https://mail.google.com/mail/u/0/#inbox"
GMAIL_DRAFTS = "https://mail.google.com/mail/u/0/#drafts"
GMAIL_SENT = "https://mail.google.com/mail/u/0/#sent"
LOGIN_URL = "https://mail.google.com/"
CALENDAR_DAY = "https://calendar.google.com/calendar/u/0/r/day/{y}/{m}/{d}"
SLOT_HOSTS = {"gmail": "mail.google.com", "calendar": "calendar.google.com"}
_MAX_URL = 2000


@dataclass(frozen=True)
class Target:
    url: str
    slot: str  # "gmail" | "calendar" | "web": which window this belongs in
    refresh: bool = False  # True after a write: reload the tab in place if the launcher can
    new_window: bool = True  # False = open as a tab of the window just opened (extra search results)


# ---------------------------------------------------------------------------- url helpers


def http_url(value: Any) -> str | None:
    """The URL if it is a plain http(s) link worth opening, else None. Search results are untrusted data."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > _MAX_URL or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    if url.endswith("...") and len(url) > 400:  # the recorder truncates long text; a cut URL is a broken URL
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or "@" in p.netloc:
        return None
    if is_internal_host(p.hostname):
        return None
    return url


_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")


def is_internal_host(host: str) -> bool:
    """True for names and addresses that point at this machine or a private network (never shown on stage).

    Pure string/IP checks, no DNS lookup: a public name that secretly resolves to a private address is not caught."""
    h = host.lower().rstrip(".").split("%")[0]
    if not h or h == "localhost" or h.endswith(_INTERNAL_SUFFIXES):
        return True
    ip = None
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        if re.fullmatch(r"[0-9a-fx.]+", h):  # legacy IPv4 spellings: 2130706433, 0x7f.1, 0177.0.0.1, 127.1
            try:
                ip = ipaddress.IPv4Address(socket.inet_aton(h))
            except (OSError, ValueError):
                ip = None
    if ip is None:
        return "." not in h  # a one-word name (intranet, router) is not a public site
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global  # loopback, 10/8, 172.16/12, 192.168/16, link-local, CGNAT, unspecified, ULA ...


def _norm(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{(p.hostname or '').lower()}{':' + str(p.port) if p.port else ''}{path}?{p.query}#{p.fragment}"


def calendar_url(day: date) -> str:
    return CALENDAR_DAY.format(y=day.year, m=day.month, d=day.day)  # Google wants no zero padding


def _parse_day(value: Any) -> date | None:
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def event_day(ev: Activity) -> date | None:
    """The calendar day an event is about: what was asked for, else what came back. None if it says nothing."""
    args = ev.detail.get("args") or {}
    candidates: list[Any] = [args.get("start")]
    fields = args.get("fields")
    if isinstance(fields, dict):
        candidates += [fields.get(k) for k in ("start", "start_datetime", "start_time")]
    result = ev.detail.get("result")
    if isinstance(result, list) and result:
        first = result[0]
        candidates.append(first.get("start") if isinstance(first, dict) else first[0] if isinstance(first, (list, tuple)) and first else None)
    elif isinstance(result, dict):
        candidates.append(result.get("start"))
    for c in candidates:
        d = _parse_day(c)
        if d:
            return d
    return None


# ---------------------------------------------------------------------------- the one mapping table
# (kind, tool) -> what to show. Reads open the app; writes (which exist only after approval, or for a draft)
# ask for a refresh of the same app. Add a row here to show something new.


def _gmail(url: str, refresh: bool = False):
    return lambda layer, ev: [Target(url, "gmail", refresh)]


def _calendar(refresh: bool):
    return lambda layer, ev: [Target(calendar_url(layer.calendar_day(ev)), "calendar", refresh)]


def _search_pages(layer: "ScreenLayer", ev: Activity) -> list[Target]:
    urls: list[str] = []
    for item in ev.detail.get("result") or []:
        url = http_url(item.get("url")) if isinstance(item, dict) else None
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= layer.max_search_urls:
            break
    return [Target(u, "web", False, new_window=(i == 0)) for i, u in enumerate(urls)]


ROUTES: dict[tuple[str, str], Callable[["ScreenLayer", Activity], list[Target]]] = {
    ("read", "fetch_emails"): _gmail(GMAIL_INBOX),
    ("read", "fetch_thread"): _gmail(GMAIL_INBOX),
    ("read", "list_events"): _calendar(False),
    ("read", "find_free_slots"): _calendar(False),
    ("write", "create_event"): _calendar(True),
    ("write", "patch_event"): _calendar(True),
    ("write", "delete_event"): _calendar(True),
    ("write", "create_draft"): _gmail(GMAIL_DRAFTS, True),
    ("write", "send_email"): _gmail(GMAIL_SENT, True),
    ("read", "search_web"): _search_pages,
}


# ---------------------------------------------------------------------------- the layer


class ScreenLayer:
    """Subscribe it to a bus: `bus.subscribe(layer)`. Never raises into Chief."""

    def __init__(
        self,
        launcher: "DryRunLauncher | ChromeLauncher",
        *,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.monotonic,
        max_per_request: int = 6,
        dedupe_seconds: float = 30.0,
        refresh_gap: float = 2.0,
        request_gap: float = 90.0,
        max_search_urls: int = 3,
        refresh_delay: float = 1.5,
        schedule: Callable[[float, Callable[[], None]], Any] | None = None,
    ):
        self.launcher = launcher
        self.today, self.clock = today, clock
        self.max_per_request, self.dedupe_seconds = max_per_request, dedupe_seconds
        self.refresh_gap, self.request_gap, self.max_search_urls = refresh_gap, request_gap, max_search_urls
        self._seen: dict[str, float] = {}
        self._refreshed: dict[str, float] = {}
        self._opened = self._refreshes = 0
        self._last_event = float("-inf")
        self._last_day: date | None = None
        self.refresh_delay = refresh_delay  # a write has just happened: give the app a moment before reloading
        self._schedule = schedule or self._timer
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()  # one launch at a time, whether from Chief or from a delayed reload

    def _timer(self, delay: float, fn: Callable[[], None]) -> None:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for delayed reloads (call before the process exits)."""
        for timer in list(self._timers):
            timer.join(timeout)

    def calendar_day(self, ev: Activity) -> date:
        day = event_day(ev) or self._last_day or self.today()
        self._last_day = day
        return day

    def plan(self, ev: Activity) -> list[Target]:
        if ev.detail.get("cached") or ev.detail.get("simulated"):
            return []  # replayed or made-up data did not come from live pages: do not pretend it did
        route = ROUTES.get((ev.kind, ev.tool or ""))
        return route(self, ev) if route and ev.ok else []

    def __call__(self, ev: Activity) -> list[Target]:
        try:
            return self._handle(ev)
        except Exception:
            return []

    def _handle(self, ev: Activity) -> list[Target]:
        now = self.clock()
        if ev.kind == "heard" or now - self._last_event > self.request_gap:
            self._opened = self._refreshes = 0  # a new request starts with a fresh budget
        self._last_event = now
        shown: list[Target] = []
        for t in self.plan(ev):
            mode = self._admit(t, now)
            if not mode:
                continue
            if mode == "refresh" and self.refresh_delay > 0:
                self._schedule(self.refresh_delay, lambda t=t: self._open(t))
                shown.append(t)
            elif self._open(t):
                shown.append(t)
        return shown

    def _admit(self, t: Target, now: float) -> str | None:
        """'refresh' (reload in place), 'open', or None (skipped by the throttle)."""
        if t.refresh and self.launcher.can_refresh:
            if now - self._refreshed.get(t.slot, float("-inf")) < self.refresh_gap or self._refreshes >= self.max_per_request:
                return None
            self._refreshes += 1
            self._refreshed[t.slot] = now
            return "refresh"
        if now - self._seen.get(_norm(t.url), float("-inf")) < self.dedupe_seconds:
            return None  # never the same page twice in a short window
        if self._opened >= self.max_per_request:
            return None
        self._opened += 1
        return "open"

    def _open(self, t: Target) -> bool:
        with self._lock:
            try:
                ok = bool(self.launcher.open(t))
            except Exception:
                ok = False
        if ok:
            self._seen[_norm(t.url)] = self.clock()
        return ok


# ---------------------------------------------------------------------------- launchers


class DryRunLauncher:
    """Records what would open; no browser. For tests and for rehearsing without a display."""

    def __init__(self, *, can_refresh: bool = False, echo: bool = False):
        self.can_refresh, self.echo = can_refresh, echo
        self.opened: list[Target] = []

    def open(self, t: Target) -> bool:
        self.opened.append(t)
        if self.echo:
            print(f"[screen] {'refresh' if t.refresh and self.can_refresh else 'open'} {t.slot}: {t.url}")
        return True

    def login(self) -> bool:
        self.opened.append(Target(LOGIN_URL, "gmail"))
        return True


class ScreenSetupError(RuntimeError):
    """The layer cannot start safely; the message says why. Chief itself carries on without windows."""


class BrowserNotFound(ScreenSetupError):
    pass


class UnsafeProfile(ScreenSetupError):
    pass


def default_profile_dirs(env: dict[str, str] | None = None, home: Path | None = None, platform: str = sys.platform) -> list[Path]:
    """Where Chrome, Edge and friends keep the user's REAL profile."""
    env = os.environ if env is None else env
    home = home or Path.home()
    if platform.startswith("win"):
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        names = ("Google/Chrome", "Google/Chrome Beta", "Google/Chrome SxS", "Microsoft/Edge", "Chromium", "BraveSoftware/Brave-Browser")
        return [local / n / "User Data" for n in names]
    if platform == "darwin":
        base = home / "Library" / "Application Support"
        return [base / n for n in ("Google/Chrome", "Microsoft Edge", "Chromium", "BraveSoftware/Brave-Browser")]
    cfg = home / ".config"
    return [cfg / n for n in ("google-chrome", "chromium", "microsoft-edge", "BraveSoftware/Brave-Browser")] + [home / "snap" / "chromium"]


def check_profile_dir(path: Path | str, env=None, home=None, platform: str = sys.platform) -> Path:
    """The dedicated profile folder, resolved; refuses anything that is or overlaps a browser's default profile."""
    prof = Path(os.path.expandvars(str(path))).expanduser().resolve()
    for default in default_profile_dirs(env, home, platform):
        d = default.resolve()
        if prof == d or d in prof.parents or prof in d.parents:
            raise UnsafeProfile(
                f"CHIEF_SHOW_PROFILE_DIR ({prof}) is, or overlaps, your normal browser profile ({d}). "
                "Pick a separate empty folder so personal accounts never appear."
            )
    if prof == Path(prof.anchor):
        raise UnsafeProfile(f"CHIEF_SHOW_PROFILE_DIR ({prof}) is a drive root; pick a dedicated folder")
    return prof


def find_browser(
    explicit: str | None = None,
    env: dict[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
) -> str:
    """CHIEF_SHOW_BROWSER, else Chrome, else Edge from their standard install paths."""
    if explicit:
        if exists(explicit) or which(explicit):
            return explicit
        raise BrowserNotFound(f"CHIEF_SHOW_BROWSER points at {explicit}, which does not exist")
    env = os.environ if env is None else env
    paths: list[str] = []
    if platform.startswith("win"):
        roots = [env.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for root in filter(None, roots):
            paths.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        for root in filter(None, roots):
            paths.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif platform == "darwin":
        paths += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    for p in paths:
        if exists(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "microsoft-edge", "msedge"):
        found = which(name)
        if found:
            return found
    raise BrowserNotFound("no Chrome or Edge found; install one or set CHIEF_SHOW_BROWSER to its full path")


def _pair(text: str | None, seps: str) -> tuple[int, int] | None:
    if not text:
        return None
    for sep in seps:
        if sep in text:
            a, _, b = text.lower().partition(sep)
            try:
                return int(a.strip()), int(b.strip())
            except ValueError:
                return None
    return None


def parse_size(text: str | None) -> tuple[int, int] | None:
    return _pair(text, "x,")  # "1280x800"


def parse_pos(text: str | None) -> tuple[int, int] | None:
    return _pair(text, ",x")  # "1920,0" (a second monitor to the right of the primary)


def browser_args(
    exe: str,
    profile_dir: Path | str,
    url: str,
    *,
    new_window: bool = True,
    size: tuple[int, int] | None = None,
    pos: tuple[int, int] | None = None,
    debug_port: int | None = None,
) -> list[str]:
    args = [exe, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check"]
    if new_window:
        args.append("--new-window")
    if size:
        args.append(f"--window-size={size[0]},{size[1]}")
    if pos:
        args.append(f"--window-position={pos[0]},{pos[1]}")
    if debug_port:
        args.append(f"--remote-debugging-port={debug_port}")  # DevTools binds to 127.0.0.1 only
    args.append(url)
    return args


class ChromeLauncher:
    def __init__(
        self,
        exe: str,
        profile_dir: Path | str,
        *,
        size: tuple[int, int] | None = None,
        pos: tuple[int, int] | None = None,
        debug_port: int | None = None,
        popen: Callable[..., Any] | None = None,
    ):
        self.exe, self.profile_dir = exe, Path(profile_dir)
        self.size, self.pos, self.debug_port, self._popen = size, pos, debug_port, popen
        self._spawned_at: float | None = None  # when we last started the browser with the DevTools port
        self._warned = False

    @property
    def can_refresh(self) -> bool:
        return bool(self.debug_port) and _cdp_alive(self.debug_port)

    def _spawn(self, url: str, *, new_window: bool = True, debug: bool = True) -> bool:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = browser_args(
            self.exe, self.profile_dir, url, new_window=new_window, size=self.size, pos=self.pos,
            debug_port=self.debug_port if debug else None,
        )
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        (self._popen or subprocess.Popen)(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
        )
        if debug and self.debug_port:
            self._spawned_at = time.monotonic()
        return True

    def _devtools_ready(self, wait: float = 4.0) -> bool:
        """Is the DevTools port up? Right after we start the browser, give it a few seconds; if it never comes up
        (for example the dedicated profile was already open without the port), say so once and use plain windows."""
        if not self.debug_port:
            return False
        if _cdp_alive(self.debug_port):
            return True
        if self._spawned_at is not None and time.monotonic() - self._spawned_at < 15:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if _cdp_alive(self.debug_port):
                    return True
        if self._spawned_at is not None and not self._warned:
            self._warned = True
            print(
                f"[screen] DevTools port {self.debug_port} is not reachable, so windows open normally and Calendar is not "
                "reloaded in place. If the dedicated profile was already open without the port, close it and start again."
            )
        return False

    def open(self, t: Target) -> bool:
        if not http_url(t.url):
            return False
        if self.debug_port and self._devtools_ready():
            try:
                if _cdp_show(self.debug_port, t, self.size, self.pos):
                    return True
            except Exception:
                pass  # fall back to a plain window
        return self._spawn(t.url, new_window=t.new_window)

    def login(self) -> bool:
        """Open the dedicated profile on Gmail so a person can sign in. No DevTools port: sign-in works best without."""
        return self._spawn(LOGIN_URL, new_window=True, debug=False)


# ---------------------------------------------------------------------------- optional: DevTools Protocol, stdlib only
# Lets a write event reload the Calendar tab in place instead of opening another window. It talks to
# 127.0.0.1 only, and only ever sends Page.navigate / Page.reload / Target.createTarget and a tab activation.


def _cdp_http(port: int, path: str, method: str = "GET", timeout: float = 1.5) -> Any:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body[:1] in (b"{", b"[") else body.decode("utf-8", "replace")


def _cdp_alive(port: int | None) -> bool:
    if not port:
        return False
    try:
        return "webSocketDebuggerUrl" in _cdp_http(port, "/json/version", timeout=0.4)
    except Exception:
        return False


def ws_frame(payload: bytes, mask_key: bytes | None = None) -> bytes:
    """One masked client text frame (RFC 6455)."""
    mask_key = mask_key or os.urandom(4)
    n = len(payload)
    head = b"\x81" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536 else b"\xff" + struct.pack(">Q", n))
    return head + mask_key + bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def _ws_read_message(rfile) -> str | None:
    data = b""
    while True:
        h = rfile.read(2)
        if len(h) < 2:
            return None
        fin, op, length = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", rfile.read(8))[0]
        mask = rfile.read(4) if h[1] & 0x80 else b""
        chunk = rfile.read(length)
        if mask:
            chunk = bytes(b ^ mask[i % 4] for i, b in enumerate(chunk))
        if op == 0x8:
            return None
        if op in (0x9, 0xA):
            continue
        data += chunk
        if fin:
            return data.decode("utf-8", "replace")


def cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 2.0) -> dict:
    u = urlparse(ws_url)
    if u.hostname not in ("127.0.0.1", "localhost", "::1") or not u.port:
        raise ValueError("DevTools endpoint must be local")
    with socket.create_connection((u.hostname, u.port), timeout=timeout) as s:
        s.settimeout(timeout)
        path = u.path + (f"?{u.query}" if u.query else "")
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        rfile = s.makefile("rb")
        status = rfile.readline()
        if b" 101 " not in status:
            raise ConnectionError(f"websocket upgrade refused: {status!r}")
        while rfile.readline() not in (b"\r\n", b""):
            pass
        s.sendall(ws_frame(json.dumps({"id": 1, "method": method, "params": params or {}}).encode()))
        while True:
            raw = _ws_read_message(rfile)
            if raw is None:
                raise ConnectionError("DevTools closed the connection")
            msg = json.loads(raw)
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})


def _cdp_show(port: int, t: Target, size: tuple[int, int] | None, pos: tuple[int, int] | None) -> bool:
    """Show target t in the already-running browser: reuse its Gmail/Calendar tab, else create one."""
    host = SLOT_HOSTS.get(t.slot)
    if host:
        for tab in _cdp_http(port, "/json/list"):
            if tab.get("type") == "page" and urlparse(tab.get("url", "")).hostname == host and tab.get("webSocketDebuggerUrl"):
                ws = tab["webSocketDebuggerUrl"]
                if _norm(tab["url"]) == _norm(t.url):
                    if t.refresh:
                        cdp_call(ws, "Page.reload", {"ignoreCache": True})  # the same page again: reload, never a new tab
                else:
                    cdp_call(ws, "Page.navigate", {"url": t.url})
                _cdp_http(port, f"/json/activate/{tab['id']}")
                return True
    version = _cdp_http(port, "/json/version")
    params: dict[str, Any] = {"url": t.url, "newWindow": t.new_window}
    if size:
        params.update(width=size[0], height=size[1])
    if pos:
        params.update(left=pos[0], top=pos[1])
    try:
        cdp_call(version["webSocketDebuggerUrl"], "Target.createTarget", params)
    except Exception:
        _cdp_http(port, "/json/new?" + quote(t.url, safe=":/?&=#%"), method="PUT")
    return True


# ---------------------------------------------------------------------------- wiring and helpers for the CLI


def build_launcher(cfg) -> "DryRunLauncher | ChromeLauncher":
    if cfg.show_dry_run:
        return DryRunLauncher(echo=True)
    exe = find_browser(cfg.show_browser)
    return ChromeLauncher(
        exe, check_profile_dir(cfg.show_profile_dir), size=parse_size(cfg.show_window_size), pos=parse_pos(cfg.show_window_pos),
        debug_port=cfg.show_debug_port,
    )


def attach(rt, cfg=None) -> ScreenLayer | None:
    """Subscribe the window layer to rt.bus. Returns None (and says why) if there is no browser to drive."""
    cfg = cfg or rt.cfg
    try:
        launcher = build_launcher(cfg)
    except ScreenSetupError as exc:
        print(f"Real windows are off: {exc}")
        return None
    layer = ScreenLayer(launcher, today=lambda: rt.now().date())
    rt.bus.subscribe(layer)
    atexit.register(layer.flush)  # a one-shot run should still do its delayed reload before exiting
    where = "dry run (printing only)" if isinstance(launcher, DryRunLauncher) else f"profile {cfg.show_profile_dir}"
    print(f"Real windows on: {where}. Sign in once with: python -m chief show-login")
    return layer


def show_login(cfg) -> int:
    try:
        launcher = build_launcher(cfg)
    except ScreenSetupError as exc:
        print(exc)
        return 1
    launcher.login()
    print(
        "A browser window opened on the dedicated Chief profile.\n"
        "Sign in to the TEST Google account only (never a personal one), by hand, then close the window.\n"
        f"The login is kept in {cfg.show_profile_dir}. Chief never types credentials."
    )
    return 0


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    w: int
    h: int
    primary: bool = False


def detect_monitors() -> list[Monitor]:
    """Monitors as Windows reports them (virtual-screen pixels, primary first). Empty off Windows."""
    if not sys.platform.startswith("win"):
        return []
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    found: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MonitorInfo)]

    def visit(hmon, _hdc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(MonitorInfo)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, callback_type(visit), 0)
    return sorted(found, key=lambda m: (not m.primary, m.x, m.y))


def suggest_layout(monitors: list[Monitor]) -> list[str]:
    """Lines for the terminal: the monitors, and CHIEF_SHOW_WINDOW_* values that put the real windows on another one."""
    if not monitors:
        return ["No monitors detected (this helper only reads them on Windows). Pick CHIEF_SHOW_WINDOW_POS by hand: x,y in desktop pixels."]
    lines = ["Monitors (x,y and size in desktop pixels):"]
    for i, m in enumerate(monitors, 1):
        lines.append(f"  {i}. {m.w}x{m.h} at {m.x},{m.y}{'  (primary)' if m.primary else ''}")
    others = [m for m in monitors if not m.primary]
    if others:
        lines.append("")
        lines.append("Keep the Stage page on the primary monitor and put the real windows on another one:")
        for i, m in enumerate(monitors, 1):
            if not m.primary:
                lines.append(f"  monitor {i}:  CHIEF_SHOW_WINDOW_POS={m.x},{m.y}   CHIEF_SHOW_WINDOW_SIZE={m.w}x{m.h}")
    else:
        m = monitors[0]
        lines += [
            "",
            "Only one monitor: share it, Stage on the left half, real windows on the right half:",
            f"  CHIEF_SHOW_WINDOW_POS={m.x + m.w // 2},{m.y}   CHIEF_SHOW_WINDOW_SIZE={m.w // 2}x{m.h}",
        ]
    lines.append("")
    lines.append("Put the Stage page on its monitor by dragging your own browser window there; Chief only places its own windows.")
    lines.append("On displays with mixed scaling (125%/150%) the numbers can be off: nudge them and run `python -m chief show-demo`.")
    return lines


def show_layout(cfg, monitors: list[Monitor] | None = None) -> int:
    print("\n".join(suggest_layout(detect_monitors() if monitors is None else monitors)))
    cur = f"CHIEF_SHOW_WINDOW_POS={cfg.show_window_pos or '(unset)'}  CHIEF_SHOW_WINDOW_SIZE={cfg.show_window_size or '(unset)'}"
    print(f"\nCurrent: {cur}")
    return 0


def demo_events(today: date) -> list[tuple[float, str, str, str | None, dict]]:
    """(pause before, kind, title, tool, detail): a canned run-through. Nothing here touches any account."""
    day = today.isoformat()
    return [
        (0, "heard", "Give me my morning brief", None, {}),
        (1, "read", "Read your email: 5 message(s)", "fetch_emails", {"result": []}),
        (4, "read", "Read your calendar: 3 event(s)", "list_events", {"args": {"start": f"{day}T00:00:00"}, "result": []}),
        (4, "read", "Searched the web: trains", "search_web", {"result": [
            {"title": "Caltrain", "url": "https://www.caltrain.com/"},
            {"title": "511", "url": "https://511.org/"},
            {"title": "not a web page", "url": "javascript:alert(1)"},
        ]}),
        (5, "write", "Updated calendar event (canned: nothing was changed)", "patch_event",
         {"args": {"event_id": "demo", "fields": {"start": f"{day}T10:00:00"}}, "result": {"updated": True}}),
    ]


def show_demo(cfg, *, sleep: Callable[[float], None] = time.sleep, interactive: bool | None = None) -> int:
    """Replay canned events through the layer so the real windows can be watched. Needs the test Google account."""
    print(
        "== chief show-demo ==\n"
        "Needs the TEST Google account signed in to the dedicated profile (python -m chief show-login).\n"
        "Replays made-up events: it opens Gmail, Google Calendar and two web pages, then a Calendar update\n"
        "(reloaded in place only if CHIEF_SHOW_DEBUG_PORT is set).\n"
        "It changes nothing in any account; real changes only ever go through the approval gate."
    )
    try:
        launcher = build_launcher(cfg)
    except ScreenSetupError as exc:
        print(exc)
        return 1
    if interactive is None:
        interactive = sys.stdin.isatty() and not cfg.show_dry_run
    if interactive:
        try:
            input("Press Enter to start, Ctrl+C to cancel. ")
        except (EOFError, KeyboardInterrupt):
            return 1
    bus = ActivityBus()
    layer = ScreenLayer(launcher)
    bus.subscribe(layer)
    for pause, kind, title, tool, detail in demo_events(date.today()):
        sleep(pause)
        print(f"  event: {kind} {tool or ''} {title}")
        bus.emit(kind, title, tool=tool, detail=detail)
    print("done.")
    return 0

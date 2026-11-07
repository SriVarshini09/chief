"""Chief's front door: a public landing page, password sign-in, and the "connect your accounts" page.

Chief is single-owner. One web token guards everything; signing in with a password just hands your
browser that same token, as the existing HttpOnly, SameSite=Strict `chief_token` cookie. So every
existing token link keeps working, and nothing behind the door had to change.

How the pages get the token for their API calls (they send it as `X-Chief-Token`): `GET /session`.
It is authenticated by the cookie and answers only same-origin requests that carry the custom header
`X-Chief-Session: 1`. Another site cannot read it: the cookie is SameSite=Strict, so a cross-site
request arrives without it; the custom header forces a CORS preflight that Chief never approves; and
`Sec-Fetch-Site` / `Origin`, when the browser sends them, must say same-origin. The token already lives
in sessionStorage on every Chief page, so a script running in the page gains nothing new from it.

Creating the first account is the dangerous moment: whoever does it owns the Chief. It is allowed only
  - with the web token (the private link Chief printed, or the `X-Chief-Token` header), or
  - from a browser on the computer running Chief: the connection comes from a loopback address, the Host
    header names localhost, and there are no proxy headers. The address alone is not enough, because a
    Cloudflare tunnel also connects from 127.0.0.1; a tunnelled request carries the public Host name and
    Cf-Connecting-Ip / X-Forwarded-For, so it is refused.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from chief.owner import OwnerError, OwnerStore
from chief.runtime import Runtime

HERE = Path(__file__).parent
WELCOME_PAGE = HERE / "welcome.html"
SIGNIN_PAGE = HERE / "signin.html"
SETUP_PAGE = HERE / "setup.html"
FRONT_CSS = HERE / "front.css"
COOKIE = "chief_token"  # the same cookie app.py sets for token links
TELEGRAM_DOC = "https://github.com/SriVarshini09/chief/blob/main/docs/telegram-setup.md"
SAMPLE_MODES = ("mock", "demo-offline")
NEXT_PAGES = ("/dashboard", "/setup", "/")  # where a sign-in may send you; nothing else (no open redirect)
SIGNED_IN_PAGES = ("/dashboard", "/setup")  # opening these signed out goes to the sign-in page
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

MAX_WRONG = 8  # wrong passwords per address within WRONG_WINDOW; after that, 429 until the window passes
WRONG_WINDOW = 900.0
BASE_DELAY = 0.5  # seconds; doubles with each recent wrong password, up to MAX_DELAY
MAX_DELAY = 4.0
STATUS_TTL = 10.0  # seconds a Composio connection check is reused, so polling stays cheap
GENERIC_WRONG = "That email and password don't match."

KEYS = [  # (name, what it is for): shown as set / not set, never the value
    ("CHIEF_MODE", "mock (sample data) or live"),
    ("COMPOSIO_API_KEY", "Gmail and Calendar through Composio"),
    ("CHIEF_USER_ID", "which Composio user owns the connections"),
    ("PARALLEL_API_KEY", "live web search"),
    ("CHIEF_LLM_API_KEY", "the model that writes summaries and answers"),
    ("CHIEF_LLM_MODEL", "model name for the LLM host"),
    ("CHIEF_LLM_BASE_URL", "OpenAI-compatible LLM host"),
    ("CHIEF_STT_API_KEY", "speech-to-text for push-to-talk"),
    ("CHIEF_WEB_TOKEN", "the private key to these pages"),
    ("TELEGRAM_BOT_TOKEN", "the Telegram bot"),
    ("TELEGRAM_USER_ID", "who the Telegram bot answers"),
    ("CHIEF_BRIEF_HOUR", "hour of the morning brief"),
    ("CHIEF_TIMEZONE", "your time zone for live calendar times"),
]


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _host_only(netloc: str) -> str:
    return (urlsplit("//" + netloc).hostname or "").lower()


def same_site(request: Request) -> bool:
    """True only for a request made by a Chief page itself. Login and sign-up POSTs need it."""
    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return False
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source or source == "null":
        return False
    there = urlsplit(source).netloc.lower()
    hosts = {request.headers.get("host", "").lower()}
    hosts |= {h.strip().lower() for h in request.headers.get("x-forwarded-host", "").split(",") if h.strip()}
    return bool(there) and there in hosts


def direct_local(request: Request) -> bool:
    """A browser on this computer talking straight to Chief, not through a tunnel or proxy."""
    try:
        loopback = ipaddress.ip_address(request.client.host if request.client else "").is_loopback
    except ValueError:
        loopback = False
    if not loopback or any(h in request.headers for h in PROXY_HEADERS):
        return False
    return _host_only(request.headers.get("host", "")) in LOCAL_HOSTS


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def key_status(env: Any = None) -> list[dict]:
    env = os.environ if env is None else env
    return [{"name": name, "why": why, "set": bool(str(env.get(name, "") or "").strip())} for name, why in KEYS]


def _page(path: Path) -> FileResponse:
    return FileResponse(path, media_type="text/html", headers=PAGE_HEADERS)


def install_front_door(app: FastAPI, rt: Runtime, token: str, require_token: Callable) -> None:
    store = OwnerStore(rt.cfg.state_dir)
    auth = [Depends(require_token)]
    wrong: dict[str, list[float]] = {}
    wrong_lock = threading.Lock()
    status_cache: dict[str, Any] = {"at": -1e9, "value": None}
    if not hasattr(app.state, "composio_factory"):
        from chief.tools.composio_client import ComposioGateway

        app.state.composio_factory = ComposioGateway  # tests swap in a fake

    def given_token(request: Request) -> str:
        return request.query_params.get("token") or request.headers.get("x-chief-token") or ""

    def has_token(request: Request) -> bool:
        return hmac.compare_digest(given_token(request).encode(), token.encode())

    def signed_in(request: Request) -> bool:
        """This browser holds the cookie, so Chief's pages open without a token in the address."""
        return hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), token.encode())

    def who(request: Request) -> str:
        return request.client.host if request.client else "?"

    def recent_wrong(request: Request) -> int:
        now = time.monotonic()
        with wrong_lock:
            kept = [t for t in wrong.get(who(request), []) if now - t < WRONG_WINDOW]
            wrong[who(request)] = kept
            return len(kept)

    async def count_wrong(request: Request) -> None:
        with wrong_lock:
            if len(wrong) > 10_000:  # never grow without bound
                wrong.clear()
            times = wrong.setdefault(who(request), [])
            times.append(time.monotonic())
            n = len(times)
        await _pause(min(BASE_DELAY * 2 ** (n - 1), MAX_DELAY))

    def refuse_cross_site(request: Request) -> None:
        if not same_site(request):
            raise HTTPException(403, "This form only works from Chief's own page.")

    async def json_body(request: Request) -> dict:
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            raise HTTPException(415, "send JSON")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "send JSON")
        return body

    def signed_in_response(request: Request, body: dict) -> JSONResponse:
        response = JSONResponse(body, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=is_https(request), path="/")
        return response

    def next_page(value: Any, default: str) -> str:
        return value if value in NEXT_PAGES else default

    # ---- opening a signed-in page while signed out goes to the sign-in page, not a JSON error -------

    @app.exception_handler(StarletteHTTPException)
    async def to_sign_in(request: Request, exc: StarletteHTTPException):
        if (exc.status_code == 401 and request.method == "GET" and request.url.path in SIGNED_IN_PAGES
                and "text/html" in request.headers.get("accept", "")):
            return RedirectResponse(f"/signin?next={request.url.path}", status_code=303)
        return await http_exception_handler(request, exc)

    # ---- public pages ---------------------------------------------------------------------------

    @app.get("/front.css")
    def front_css() -> FileResponse:
        return FileResponse(FRONT_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/welcome")
    def welcome_page() -> FileResponse:
        return _page(WELCOME_PAGE)

    @app.get("/signin")
    def signin_page() -> FileResponse:
        return _page(SIGNIN_PAGE)

    @app.get("/signin/state")
    def signin_state(request: Request) -> JSONResponse:
        """What the sign-in page should show. Says nothing about who the owner is."""
        return JSONResponse(
            {"owner": store.exists(), "can_create": has_token(request) or direct_local(request), "signed_in": signed_in(request)},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/signin/create")
    async def create_owner(request: Request) -> JSONResponse:
        refuse_cross_site(request)
        body = await json_body(request)
        if store.exists():
            raise HTTPException(409, "This Chief already has an account. Sign in instead.")
        if given_token(request) and not has_token(request):
            await count_wrong(request)
            raise HTTPException(401, "That private link is not right. Open the link Chief printed.")
        if not (has_token(request) or direct_local(request)):
            raise HTTPException(403, "Create your account on the computer running Chief, or open the private link Chief printed.")
        try:
            owner = await run_in_threadpool(store.create, body.get("name"), body.get("email"), body.get("password"))
        except OwnerError as exc:
            raise HTTPException(400, str(exc)) from None
        return signed_in_response(request, {"ok": True, "name": owner.name, "next": next_page(body.get("next"), "/setup")})

    @app.post("/signin")
    async def sign_in(request: Request) -> JSONResponse:
        refuse_cross_site(request)
        body = await json_body(request)
        if recent_wrong(request) >= MAX_WRONG:
            raise HTTPException(429, "Too many tries. Wait a few minutes, then try again.")
        email, password = body.get("email"), body.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            raise HTTPException(400, "Type your email and password.")
        owner = await run_in_threadpool(store.verify, email, password)
        if owner is None:
            await count_wrong(request)
            raise HTTPException(401, GENERIC_WRONG)
        with wrong_lock:
            wrong.pop(who(request), None)
        return signed_in_response(request, {"ok": True, "name": owner.name, "next": next_page(body.get("next"), "/dashboard")})

    @app.post("/signout")
    def sign_out(request: Request) -> JSONResponse:
        refuse_cross_site(request)
        response = JSONResponse({"ok": True, "next": "/welcome"}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=is_https(request))
        return response

    @app.get("/session", dependencies=auth)
    def session(request: Request) -> JSONResponse:
        """Hands the token to Chief's own page JS after a password sign-in (see the module docstring)."""
        fetch_site = request.headers.get("sec-fetch-site", "")
        origin = request.headers.get("origin", "")
        if request.headers.get("x-chief-session") != "1" or (fetch_site and fetch_site != "same-origin") or (origin and not same_site(request)):
            raise HTTPException(403, "only Chief's own pages can ask for this")
        owner = store.get()
        return JSONResponse({"token": token, "owner": owner.public() if owner else None}, headers={"Cache-Control": "no-store"})

    # ---- setup: connect your accounts -----------------------------------------------------------

    @app.get("/setup", dependencies=auth)
    def setup_page() -> FileResponse:
        return _page(SETUP_PAGE)

    def google_status(fresh: bool) -> dict:
        mode = rt.cfg.mode
        if mode in SAMPLE_MODES:
            return {"state": "sample", "mode": mode, "toolkits": {}}
        if not os.getenv("COMPOSIO_API_KEY", "").strip():
            return {"state": "no_key", "mode": mode, "toolkits": {}}
        now = time.monotonic()
        if not fresh and status_cache["value"] is not None and now - status_cache["at"] < STATUS_TTL:
            return status_cache["value"]
        try:
            found = app.state.composio_factory().connection_status()
            value = {"state": "connected" if found and all(found.values()) else "not_connected", "mode": mode,
                     "toolkits": {str(k): bool(v) for k, v in found.items()}}
        except Exception as exc:  # the SDK's own errors too; only the type is shown, never a message that could echo a key
            value = {"state": "error", "mode": mode, "toolkits": {}, "detail": type(exc).__name__}
        status_cache.update(at=now, value=value)
        return value

    @app.get("/setup/status", dependencies=auth)
    async def setup_status(fresh: int = 0) -> JSONResponse:
        owner = store.get()
        google = await run_in_threadpool(google_status, bool(fresh))
        tg_owner = os.getenv("TELEGRAM_USER_ID", "").strip()
        return JSONResponse({
            "mode": rt.cfg.mode,
            "owner": owner.public() if owner else None,
            "google": google,
            "telegram": {"bot": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()), "owner": tg_owner.isdigit(), "doc": TELEGRAM_DOC},
            "location": {"shared": rt.where.here(rt.now()) is not None},
            "brief": {"hour": rt.cfg.brief_hour, "timezone": os.getenv("CHIEF_TIMEZONE", "").strip() or None,
                      "computer_zone": time.strftime("%Z") or None},
            "keys": key_status(),
        }, headers={"Cache-Control": "no-store"})

    @app.post("/setup/connect/{toolkit}", dependencies=auth)
    async def connect(toolkit: str) -> dict:
        """A Composio link that you open yourself, in a new tab, to let Chief read Gmail or Calendar."""
        if toolkit not in ("gmail", "calendar"):
            raise HTTPException(404, "connect gmail or calendar")
        google = google_status(False)
        if google["state"] == "sample":
            raise HTTPException(409, "Chief is using sample data. Set CHIEF_MODE=live in .env to connect your real Google account.")
        if google["state"] == "no_key":
            raise HTTPException(409, "Add COMPOSIO_API_KEY to .env first, then restart Chief.")
        try:
            url = await run_in_threadpool(lambda: app.state.composio_factory().connect_link(toolkit))
        except Exception as exc:
            raise HTTPException(502, f"Composio could not make a link ({type(exc).__name__}).") from exc
        if not isinstance(url, str) or not url.startswith("https://"):
            raise HTTPException(502, "Composio did not return a usable link.")
        status_cache["value"] = None  # look again after you come back
        return {"url": url}

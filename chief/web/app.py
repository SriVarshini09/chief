"""FastAPI app behind the phone voice page.

Every route needs the access token (CHIEF_WEB_TOKEN), sent as `?token=` or an `X-Chief-Token` /
`Authorization: Bearer` header. Progress is shown by polling /status: Cloudflare quick tunnels do
not carry Server-Sent Events.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from chief import guide as walking_guide
from chief.dashboard import snapshot
from chief.gate import results_text
from chief.intent import handle_utterance
from chief.models import Proposal
from chief.runtime import Runtime

PAGE = Path(__file__).parent / "index.html"
STAGE_PAGE = Path(__file__).parent / "stage.html"
DASHBOARD_PAGE = Path(__file__).parent / "dashboard.html"
WORLD_SCRIPT = Path(__file__).parent / "world.js"
PET_SCRIPT = Path(__file__).parent / "pet.js"
GUIDE_SCRIPT = Path(__file__).parent / "guide.js"
FOOTAGE = Path(__file__).parent / "footage"
OWN_PICTURE = "background.jpg"
FOOTAGE_FILES = {
    "daisies.mp4": "video/mp4",
    "daisies.jpg": "image/jpeg",
    "orchard.jpg": "image/jpeg",
    "sunset-clouds.mp4": "video/mp4",
    "sunset-clouds.jpg": "image/jpeg",
}
ICONS = Path(__file__).parent / "icons"
ICON_NAMES = {"icon-192.png", "icon-512.png", "apple-touch-icon.png"}
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 2000
COOKIE = "chief_token"
SAFETY_HEADERS = {
    "Referrer-Policy": "no-referrer",  # links you open never learn the page address
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",  # no other site can show Chief inside a frame and trick a click on Approve
    "Content-Security-Policy": "frame-ancestors 'none'",
}
MAX_WRONG_TOKENS = 20  # per address, within GUESS_WINDOW seconds; then 429 until the window passes
GUESS_WINDOW = 300.0


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, filename: str, content_type: str) -> str: ...


class WhisperTranscriber:
    """Any OpenAI-compatible /audio/transcriptions endpoint (Groq by default)."""

    def __init__(self, base_url: str, api_key: str | None, model: str, timeout: float = 30.0):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    @classmethod
    def from_env(cls) -> "WhisperTranscriber":
        return cls(
            os.getenv("CHIEF_STT_BASE_URL") or "https://api.groq.com/openai/v1",
            os.getenv("CHIEF_STT_API_KEY") or None,
            os.getenv("CHIEF_STT_MODEL") or "whisper-large-v3-turbo",
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        resp = httpx.post(
            f"{self.base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"model": self.model, "response_format": "json"},
            files={"file": (filename, audio, content_type)},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()


def _extension(content_type: str) -> str:
    ct = content_type.lower()
    for key, ext in (("webm", "webm"), ("ogg", "ogg"), ("mp4", "mp4"), ("m4a", "m4a"), ("mpeg", "mp3"), ("wav", "wav")):
        if key in ct:
            return ext
    return "webm"


def proposal_json(p: Proposal, rt: Runtime) -> dict:
    skill = rt.skills.get(p.skill)
    return {
        "id": p.id,
        "skill": p.skill,
        "label": (skill.label if skill else "") or p.skill,
        "summary": p.summary,
        "plain": rt.gate.plain(p),  # what the person reads before approving, naming the events it touches
        "actions": [a.describe() for a in p.actions],  # the exact calls, behind "Exact details"
        "sources": list(p.sources),
        "links": [dict(link) for link in p.links],  # things you open yourself: a map, a booking page
        "items": [dict(item) for item in p.items],  # a list to show, like places nearby
        "needs": p.needs,  # "location": ask the browser where you are, then ask again
    }


class WebState:
    """What /status reports: the latest steps and spoken reply, plus a busy flag."""

    def __init__(self) -> None:
        self.lock: threading.RLock | None = None  # shared with Telegram: one turn touches the runtime at a time
        self.busy = False
        self.heard = ""
        self.say = ""
        self.steps: list[str] = []
        self.seq = 0  # bumps on every new reply so the page can tell a fresh one from a repeat


def create_app(rt: Runtime, token: str, transcriber: Transcriber | None = None) -> FastAPI:
    if not token:
        raise ValueError("CHIEF_WEB_TOKEN is required: this page can approve actions on your accounts")
    stt = transcriber if transcriber is not None else WhisperTranscriber.from_env()
    state = WebState()
    state.lock = rt.turn_lock
    app = FastAPI(title="Chief", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        response = await call_next(request)
        for name, value in SAFETY_HEADERS.items():
            response.headers.setdefault(name, value)
        given = request.query_params.get("token", "")
        if given and response.status_code < 400 and hmac.compare_digest(given.encode(), token.encode()):
            response.set_cookie(  # so a reload works after the page takes the token out of the address bar
                COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/",
            )
        return response

    misses: dict[str, list[float]] = {}  # client address -> times of wrong tokens
    misses_lock = threading.Lock()

    def require_token(request: Request) -> None:
        who = request.client.host if request.client else "?"
        now = time.monotonic()
        with misses_lock:
            recent = [t for t in misses.get(who, []) if now - t < GUESS_WINDOW]
            misses[who] = recent
            if len(recent) >= MAX_WRONG_TOKENS:
                raise HTTPException(status_code=429, detail="too many wrong tokens; wait a few minutes")
        given = request.query_params.get("token") or request.headers.get("x-chief-token") or ""
        bearer = request.headers.get("authorization", "")
        if not given and bearer.lower().startswith("bearer "):
            given = bearer[7:].strip()
        if not given and request.method == "GET":
            # Reloading a page whose address no longer shows the token. Only for reading: anything that
            # approves, asks or changes still needs the token itself, so the cookie can't be used against you.
            given = request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            with misses_lock:
                misses.setdefault(who, []).append(now)
                if len(misses) > 10_000:  # never grow without bound
                    misses.clear()
            raise HTTPException(status_code=401, detail="missing or wrong token")

    auth = [Depends(require_token)]

    def reply(say: str, steps: list[str]) -> None:
        state.say, state.steps = say, steps
        state.seq += 1

    @app.get("/", dependencies=auth)
    def page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/manifest.webmanifest", dependencies=auth)
    def manifest() -> JSONResponse:
        """What "Add to Home Screen" reads. It holds the token because the installed app opens without
        a link to click, so treat an installed phone like the private link itself."""
        query = f"?token={quote(token, safe='')}"
        return JSONResponse(
            {
                "name": "Chief",
                "short_name": "Chief",
                "description": "Your personal agent. It changes nothing until you approve.",
                "id": "/",
                "start_url": f"/{query}",
                "scope": "/",
                "display": "standalone",
                "background_color": "#0f1115",
                "theme_color": "#0f1115",
                "icons": [
                    {"src": f"/icons/icon-{size}.png{query}", "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any maskable"}
                    for size in (192, 512)
                ],
            },
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/icons/{name}", dependencies=auth)
    def icon(name: str) -> FileResponse:
        if name not in ICON_NAMES:
            raise HTTPException(404, "no such icon")
        return FileResponse(ICONS / name, media_type="image/png")

    @app.post("/stt", dependencies=auth)
    async def speech_to_text(audio: UploadFile = File(...)) -> dict:
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if not data:
            raise HTTPException(400, "empty recording")
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "recording too long")
        if getattr(stt, "enabled", True) is False:
            raise HTTPException(503, "speech-to-text is not configured (set CHIEF_STT_API_KEY)")
        ctype = audio.content_type or "audio/webm"
        try:
            text = await run_in_threadpool(stt.transcribe, data, f"speech.{_extension(ctype)}", ctype)
        except Exception as exc:
            raise HTTPException(502, f"transcription failed: {type(exc).__name__}") from exc
        return {"text": text}

    @app.post("/ask", dependencies=auth)
    def ask(body: dict) -> dict:
        raw = body.get("text")
        text = raw.strip()[:MAX_TEXT_CHARS] if isinstance(raw, str) else ""  # null or a number is not something said
        if not text:
            raise HTTPException(400, "text is required")
        with state.lock:
            state.busy, state.heard, state.steps = True, text, ["Thinking"]
            try:
                out = handle_utterance(rt, text)
            except Exception as exc:
                reply("Sorry, something went wrong.", [f"Error: {type(exc).__name__}"])
                raise HTTPException(500, "handler failed") from exc
            finally:
                state.busy = False
            reply(out["say"], list(out["steps"]))
            proposal = out.get("proposal")
            return {
                "say": out["say"],
                "steps": list(out["steps"]),
                "proposal": proposal_json(proposal, rt) if proposal else None,
            }

    def decide(proposal_id: str, approve: bool) -> dict:
        with state.lock:
            if proposal_id not in rt.gate.pending:
                raise HTTPException(404, "no such pending proposal")
            if approve:
                results = rt.gate.approve(proposal_id)
                failed = [r for r in results if "error" in r]
                say, steps = results_text(results), ["Applying changes", "Failed" if failed else "Done"]
            else:
                rt.gate.skip(proposal_id)
                results, say, steps = [], "Okay, skipped.", ["Skipped"]
            reply(say, steps)
            return {"say": say, "steps": steps, "results": results}

    @app.post("/approve/{proposal_id}", dependencies=auth)
    def approve(proposal_id: str) -> dict:
        return decide(proposal_id, True)

    @app.post("/skip/{proposal_id}", dependencies=auth)
    def skip(proposal_id: str) -> dict:
        return decide(proposal_id, False)

    @app.post("/undo/{mark}", dependencies=auth)
    def undo(mark: str) -> dict:
        with state.lock:
            say = rt.gate.undo(mark)
            if say is None:
                raise HTTPException(409, "That can no longer be undone.")
            reply(say, ["Undone"])
            return {"say": say}

    # ---- where you are: kept in memory only, never on disk ---------------------------------------------

    @app.post("/location", dependencies=auth)
    def share_location(body: dict) -> dict:
        try:
            lat, lon = float(body.get("lat")), float(body.get("lon"))
            here = rt.where.set(lat, lon, label="", source="browser", now=rt.now())
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "lat and lon must be numbers on Earth") from exc
        return {"ok": True, "source": here.source}

    @app.delete("/location", dependencies=auth)
    def forget_location() -> dict:
        rt.where.clear()
        return {"ok": True}

    # ---- walking guidance: turn by turn from your live location (chief/guide.py), memory only --------

    @app.get("/guide.js", dependencies=auth)
    def guide_script() -> FileResponse:
        return FileResponse(GUIDE_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    def _position(body: dict) -> tuple[float, float, float | None]:
        try:
            lat, lon = float(body.get("lat")), float(body.get("lon"))
            rt.where.set(lat, lon, label="", source="browser", now=rt.now())  # guiding also keeps "Around you" current
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "lat and lon must be numbers on Earth") from exc
        acc = body.get("accuracy")
        return lat, lon, float(acc) if isinstance(acc, (int, float)) and not isinstance(acc, bool) else None

    @app.post("/guide/start", dependencies=auth)
    def guide_start(body: dict) -> dict:
        lat, lon, acc = _position(body)
        to = body.get("to") if isinstance(body.get("to"), dict) else None
        return walking_guide.start(rt.where, rt.tools, lat, lon, to, acc)

    @app.post("/guide/tick", dependencies=auth)
    def guide_tick(body: dict) -> dict:
        lat, lon, acc = _position(body)
        return walking_guide.tick(rt.where, rt.tools, lat, lon, acc)

    @app.post("/guide/stop", dependencies=auth)
    def guide_stop() -> dict:
        return walking_guide.stop(rt.where)

    @app.get("/status", dependencies=auth)
    def status() -> dict:
        return {
            "undo": rt.gate.undo_state(),
            "mode": rt.cfg.mode,
            "busy": state.busy,
            "heard": state.heard,
            "say": state.say,
            "seq": state.seq,
            "steps": state.steps,
            "pending": [proposal_json(p, rt) for p in list(rt.gate.pending.values())],
            "stt": getattr(stt, "enabled", True),
            "location": rt.where.here(rt.now()) is not None,
        }

    # ---- the garden dashboard: today, the week, mail and approvals in one view --------------------

    @app.get("/dashboard", dependencies=auth)
    def dashboard_page() -> FileResponse:
        return FileResponse(DASHBOARD_PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/dashboard/world.js", dependencies=auth)
    def dashboard_world() -> FileResponse:
        return FileResponse(WORLD_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/dashboard/pet.js", dependencies=auth)
    def dashboard_pet() -> FileResponse:
        return FileResponse(PET_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/dashboard/footage/{name}", dependencies=auth)
    def dashboard_footage(name: str) -> FileResponse:
        """FileResponse answers Range requests, which iPhones need before they will play a video."""
        if name == OWN_PICTURE and (FOOTAGE / name).is_file():  # optional, and never in the repo
            return FileResponse(FOOTAGE / name, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        if name not in FOOTAGE_FILES:
            raise HTTPException(404, "no such footage")
        return FileResponse(FOOTAGE / name, media_type=FOOTAGE_FILES[name], headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/dashboard/data", dependencies=auth)
    def dashboard_data() -> dict:
        return snapshot(rt)

    # ---- the Stage: a big-screen view of everything Chief does -----------------------------------

    @app.get("/stage", dependencies=auth)
    def stage_page() -> FileResponse:
        return FileResponse(STAGE_PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/stage/events", dependencies=auth)
    def stage_events(since: int = 0) -> dict:
        return {
            "events": [e.to_json() for e in rt.bus.since(max(since, 0))],
            "last_seq": rt.bus.last_seq,  # lower than the page's cursor means the server restarted
            "busy": state.busy,
            "mode": rt.cfg.mode,
            "pending": [proposal_json(p, rt) for p in list(rt.gate.pending.values())],
        }

    @app.get("/stage/calendar", dependencies=auth)
    def stage_calendar(day: str = "", event: str = "") -> dict:
        """Timed events for the next day that has any, plus the day before. Reads through the unrecorded
        tools, so polling it never fills the activity stream. `day` (YYYY-MM-DD) or `event` (an event id)
        pins the view to a particular day, e.g. the one Chief is about to change."""
        return stage_days(rt, day=day, event=event)

    return app


def _midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _calendar_event(e) -> dict:
    return {"id": e.id, "title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat(), "minutes": e.minutes}


def stage_days(rt: Runtime, day: str = "", event: str = "") -> dict:
    today = _midnight(rt.now())
    tools = rt.inner_tools
    timed = lambda evs: [e for e in evs if 0 < (e.end - e.start).total_seconds() < 24 * 3600]  # noqa: E731

    focus: datetime | None = None
    if day:
        try:
            focus = _midnight(datetime.fromisoformat(day))
        except ValueError:
            raise HTTPException(400, "day must be YYYY-MM-DD") from None
    elif event:
        found = next((e for e in tools.list_events(today - timedelta(days=7), today + timedelta(days=21)) if e.id == event), None)
        if found:
            focus = _midnight(found.start)
    pinned = focus is not None  # False: nothing was asked for, or the event id was not found
    if focus is None:
        ahead = timed(tools.list_events(today, today + timedelta(days=14)))
        focus = min((_midnight(e.start) for e in ahead), default=today)  # the next day that has events

    before = focus - timedelta(days=1)
    evs = timed(tools.list_events(before, focus + timedelta(days=1)))
    days = []
    for d in (before, focus):
        mine = [e for e in evs if _midnight(e.start) == d]
        days.append({"date": d.date().isoformat(), "label": d.strftime("%A %b %d").replace(" 0", " "), "events": [_calendar_event(e) for e in mine]})
    return {"focus": focus.date().isoformat(), "pinned": pinned, "today": today.date().isoformat(), "days": days}


def serve(rt: Runtime, token: str, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(rt, token), host=host, port=port, log_level="warning")

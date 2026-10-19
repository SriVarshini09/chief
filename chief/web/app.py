"""FastAPI app behind the phone voice page.

Every route needs the access token (CHIEF_WEB_TOKEN), sent as `?token=` or an `X-Chief-Token` /
`Authorization: Bearer` header. Progress is shown by polling /status: Cloudflare quick tunnels do
not carry Server-Sent Events.
"""

from __future__ import annotations

import hmac
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from chief.intent import handle_utterance
from chief.models import Proposal
from chief.runtime import Runtime

PAGE = Path(__file__).parent / "index.html"
STAGE_PAGE = Path(__file__).parent / "stage.html"
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 2000


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


def proposal_json(p: Proposal) -> dict:
    return {
        "id": p.id,
        "skill": p.skill,
        "summary": p.summary,
        "actions": [a.describe() for a in p.actions],
        "sources": list(p.sources),
    }


class WebState:
    """What /status reports: the latest steps and spoken reply, plus a busy flag."""

    def __init__(self) -> None:
        self.lock = threading.Lock()  # one request touches the runtime at a time
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
    app = FastAPI(title="Chief", docs_url=None, redoc_url=None, openapi_url=None)

    def require_token(request: Request) -> None:
        given = request.query_params.get("token") or request.headers.get("x-chief-token") or ""
        bearer = request.headers.get("authorization", "")
        if not given and bearer.lower().startswith("bearer "):
            given = bearer[7:].strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    auth = [Depends(require_token)]

    def reply(say: str, steps: list[str]) -> None:
        state.say, state.steps = say, steps
        state.seq += 1

    @app.get("/", dependencies=auth)
    def page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})

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
        text = str(body.get("text", "")).strip()[:MAX_TEXT_CHARS]
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
                "proposal": proposal_json(proposal) if proposal else None,
            }

    def decide(proposal_id: str, approve: bool) -> dict:
        with state.lock:
            if proposal_id not in rt.gate.pending:
                raise HTTPException(404, "no such pending proposal")
            if approve:
                results = rt.gate.approve(proposal_id)
                failed = [r for r in results if "error" in r]
                if failed:
                    say, steps = f"That didn't go through: {failed[0]['error']}", ["Applying changes", "Failed"]
                else:
                    say, steps = f"Done. {len(results)} change(s) made.", ["Applying changes", "Done"]
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

    @app.get("/status", dependencies=auth)
    def status() -> dict:
        return {
            "mode": rt.cfg.mode,
            "busy": state.busy,
            "heard": state.heard,
            "say": state.say,
            "seq": state.seq,
            "steps": state.steps,
            "pending": [proposal_json(p) for p in list(rt.gate.pending.values())],
            "stt": getattr(stt, "enabled", True),
        }

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
            "pending": [proposal_json(p) for p in list(rt.gate.pending.values())],
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

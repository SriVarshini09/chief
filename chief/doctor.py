"""`python -m chief doctor`: a preflight checklist for demo day, with a fix hint for everything that is not green.

Rules this module keeps:
  - It prints the NAMES of environment variables that are set, never their values.
  - It installs and downloads nothing, and never creates .env.
  - Nothing touches the network unless asked: the Composio connection check runs by default (it is the one
    thing that tells you the Google accounts are linked) but `--offline` skips it, and `--live` additionally
    makes one tiny call each to Parallel, the LLM and the speech-to-text host. Every network call has a timeout.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from chief.config import ROOT, Config

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
TAGS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[info]"}
MODES = ("mock", "live", "demo", "demo-offline")
STALE_AFTER = timedelta(hours=24)
NET_TIMEOUT = 8.0

# (module to import, pip name, needed for) for the `live` extra
EXTRA_PACKAGES = [
    ("fastapi", "fastapi", "web"),
    ("uvicorn", "uvicorn", "web"),
    ("multipart", "python-multipart", "web"),
    ("httpx", "httpx", "web"),
    ("openai", "openai", "llm"),
    ("composio", "composio", "live"),
    ("parallel", "parallel-web", "live"),
    ("slack_bolt", "slack-bolt", "slack"),
]

# env var, what it is for, needed in which modes ("" = optional)
ENV_VARS = [
    ("CHIEF_MODE", "mock | live | demo | demo-offline", ""),
    ("COMPOSIO_API_KEY", "Gmail and Calendar through Composio", "live demo"),
    ("CHIEF_USER_ID", "which Composio user owns the connections", ""),
    ("PARALLEL_API_KEY", "live web search (Caltrain, boba, company research)", "live demo"),
    ("CHIEF_LLM_API_KEY", "the model that phrases summaries and answers open questions", ""),
    ("CHIEF_LLM_MODEL", "model name for the LLM host", ""),
    ("CHIEF_LLM_BASE_URL", "OpenAI-compatible LLM host", ""),
    ("CHIEF_STT_API_KEY", "speech-to-text for the phone's push-to-talk", ""),
    ("CHIEF_WEB_TOKEN", "phone/Stage access token (demo makes one if unset)", ""),
    ("SLACK_BOT_TOKEN", "Slack approvals (optional)", ""),
]


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    critical: bool = False  # a FAIL here blocks `python -m chief demo`

    @property
    def blocking(self) -> bool:
        return self.critical and self.status == FAIL


@dataclass
class Deps:
    """Everything the checks look at, replaceable in tests so no real environment is needed."""

    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    version: tuple[int, int, int] = field(default_factory=lambda: tuple(sys.version_info[:3]))  # type: ignore[assignment]
    in_venv: Callable[[], bool] = lambda: sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    has_module: Callable[[str], bool] = lambda name: importlib.util.find_spec(name) is not None
    which: Callable[[str], str | None] = shutil.which
    port_free: Callable[[int], bool] = lambda port: _port_free(port)
    find_browser: Callable[[str | None], str] | None = None
    exists: Callable[[Path], bool] = lambda p: Path(p).exists()
    now: Callable[[], datetime] = datetime.now
    composio_status: Callable[[], dict[str, bool]] | None = None
    live_calls: dict[str, Callable[[], str]] | None = None  # name -> callable returning a short detail, raising on failure
    platform: str = sys.platform

    def browser(self, explicit: str | None) -> str:
        if self.find_browser is not None:
            return self.find_browser(explicit)
        from chief import screen

        return screen.find_browser(explicit, env=self.env)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0  # nothing is listening there


def _timed(fn: Callable[[], Any], timeout: float | None = None) -> Any:
    timeout = timeout or NET_TIMEOUT
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout)
    except FutureTimeout:
        raise TimeoutError(f"no answer within {timeout:g}s") from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def human_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 90:
        return f"{max(secs, 0)}s"
    if secs < 5400:
        return f"{secs // 60} min"
    if secs < 36 * 3600:
        return f"{secs / 3600:.1f} h"
    return f"{secs // 86400} days"


# --------------------------------------------------------------------------- real network probes


def _real_composio_status(cfg: Config) -> dict[str, bool]:
    from chief.tools.composio_client import ComposioGateway

    return ComposioGateway().connection_status()


def real_live_calls(cfg: Config, env: dict[str, str]) -> dict[str, Callable[[], str]]:
    """One tiny call each. Each returns a short detail or raises. Nothing is written anywhere."""

    def parallel() -> str:
        from chief.tools.live import LiveTools

        hits = LiveTools(cfg).search_web("connectivity check", ["connectivity check"], 1)
        return f"search returned {len(hits)} result(s)"

    def llm() -> str:
        from openai import OpenAI

        client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, timeout=NET_TIMEOUT)
        client.chat.completions.create(model=cfg.llm_model, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        return f"{cfg.llm_model} answered"

    def stt() -> str:
        import httpx

        base = (env.get("CHIEF_STT_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {env.get('CHIEF_STT_API_KEY', '')}"}, timeout=NET_TIMEOUT)
        r.raise_for_status()
        return "speech-to-text host accepted the key"

    return {"parallel": parallel, "llm": llm, "stt": stt}


# --------------------------------------------------------------------------- the checks


def run_checks(cfg: Config, deps: Deps | None = None, *, live: bool = False, offline: bool = False, port: int = 8765) -> list[Check]:
    d = deps or Deps()
    env = d.env
    mode = cfg.mode
    needs_live = mode in ("live", "demo")
    soft = WARN if needs_live else INFO  # things only a live run cares about are just notes in mock/offline modes
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, **k))  # noqa: E731

    # -- python and environment
    v = d.version
    add("Python version", OK if v >= (3, 11) else FAIL, f"{v[0]}.{v[1]}.{v[2]}", "" if v >= (3, 11) else "Chief needs Python 3.11 or newer; install it and recreate .venv", critical=True)
    in_venv = d.in_venv()
    add("Virtual environment", OK if in_venv else WARN, "running inside a venv" if in_venv else "not running inside a venv",
        "" if in_venv else r"use .venv\Scripts\python.exe -m chief ... (create it with: python -m venv .venv)")
    add("Mode", OK if mode in MODES else FAIL, f"CHIEF_MODE={mode}", "" if mode in MODES else f"set CHIEF_MODE to one of: {', '.join(MODES)}", critical=True)
    add(".env file", INFO, "found" if d.exists(ROOT / ".env") else "not found (environment variables can still come from the shell)")

    # -- packages
    for module, pip_name, why in EXTRA_PACKAGES:
        have = d.has_module(module)
        need = why == "web" or (why == "live" and needs_live)
        add(f"Package {pip_name}", OK if have else (FAIL if need else soft), "installed" if have else f"missing (needed for {why})",
            "" if have else 'pip install -e ".[live]"', critical=need)
    if d.platform.startswith("win") and not d.has_module("tzdata"):
        add("Package tzdata", soft, "missing (only needed if CHIEF_TIMEZONE is set)", 'pip install -e ".[live]"')
    add("QR code package", INFO, "qrcode or segno found: the phone URL will print as a QR code" if (d.has_module("qrcode") or d.has_module("segno"))
        else "not installed (optional): the phone URL prints as text only", "")

    # -- keys: names only
    for name, why, needed_in in ENV_VARS:
        is_set = bool(env.get(name, "").strip())
        required = mode in needed_in.split()
        if is_set:
            add(f"Env {name}", OK, f"set - {why}")
        elif required:
            add(f"Env {name}", FAIL, f"NOT set - {why}", f"add {name}=... to .env (see .env.example)", critical=True)
        else:
            quiet = name in ("CHIEF_MODE", "CHIEF_USER_ID", "SLACK_BOT_TOKEN", "CHIEF_WEB_TOKEN")
            add(f"Env {name}", INFO if quiet else soft, f"not set - {why}", "" if quiet else f"optional; add {name}=... to .env if you want it")

    # -- Composio connections
    if needs_live and not offline:
        if not env.get("COMPOSIO_API_KEY", "").strip():
            add("Composio connections", FAIL, "cannot check: COMPOSIO_API_KEY is not set", "set COMPOSIO_API_KEY in .env", critical=True)
        elif not d.has_module("composio") and d.composio_status is None:
            add("Composio connections", FAIL, "cannot check: composio is not installed", 'pip install -e ".[live]"', critical=True)
        else:
            try:
                probe = d.composio_status or (lambda: _real_composio_status(cfg))
                status = _timed(probe)
                missing = sorted(k for k, ok in status.items() if not ok)
                add("Composio connections", FAIL if missing else OK, ("NOT connected: " + ", ".join(missing)) if missing else "connected: " + ", ".join(sorted(status)),
                    "python -m chief.tools.composio_client gmail  (and: calendar), open the link signed in to the TEST Google account" if missing else "", critical=True)
            except Exception as exc:
                add("Composio connections", FAIL, f"check failed: {type(exc).__name__}: {str(exc)[:120]}", "check the Wi-Fi and COMPOSIO_API_KEY, then run: python -m chief.tools.composio_client --status", critical=True)
    elif needs_live:
        add("Composio connections", INFO, "skipped (--offline)")

    # -- one tiny call each (--live)
    if live and not offline:
        calls = d.live_calls if d.live_calls is not None else real_live_calls(cfg, env)
        for name, label, have_key in (
            ("parallel", "Parallel search", bool(env.get("PARALLEL_API_KEY", "").strip())),
            ("llm", "LLM host", bool(cfg.llm_api_key and cfg.llm_model)),
            ("stt", "Speech-to-text host", bool(env.get("CHIEF_STT_API_KEY", "").strip())),
        ):
            if not have_key:
                add(f"Live call: {label}", INFO, "skipped: no key configured")
                continue
            try:
                add(f"Live call: {label}", OK, _timed(calls[name]))
            except Exception as exc:
                add(f"Live call: {label}", WARN if name != "parallel" else FAIL, f"{type(exc).__name__}: {str(exc)[:120]}",
                    "check the key, the host and the Wi-Fi; the demo can fall back to the cache for reads", critical=(name == "parallel" and mode in ("live", "demo")))

    # -- tunnel, port, browser
    tunnel = d.which("cloudflared")
    add("cloudflared (phone tunnel)", OK if tunnel else WARN, "installed" if tunnel else "not installed",
        "" if tunnel else "winget install --id Cloudflare.cloudflared  (the phone mic needs HTTPS; Chief never installs it for you)")
    free = d.port_free(port)
    add(f"Port {port}", OK if free else FAIL, "free" if free else "already in use", "" if free else f"stop whatever uses it, or pick another with --port", critical=True)
    if cfg.show_windows:
        try:
            add("Browser for real windows", OK, d.browser(cfg.show_browser))
        except Exception as exc:
            add("Browser for real windows", FAIL, str(exc)[:160], "install Chrome or Edge, or set CHIEF_SHOW_BROWSER to its full path")
    else:
        try:
            add("Browser for real windows", INFO, f"found {d.browser(cfg.show_browser)} (real-window layer is off; --show-windows turns it on)")
        except Exception as exc:
            add("Browser for real windows", INFO, f"not found ({str(exc)[:80]}); only matters with --show-windows")

    # -- demo cache
    add_cache_check(out, cfg, d, mode)
    return out


def add_cache_check(out: list[Check], cfg: Config, d: Deps, mode: str) -> None:
    from chief.tools.replay import DemoCache

    path = cfg.cache_path
    must = mode == "demo-offline"
    quiet = WARN if mode in ("live", "demo") else INFO
    if not d.exists(path):
        out.append(Check("Demo cache", FAIL if must else quiet, f"{path} not found - there is no safety net if the Wi-Fi drops",
                         "while online, run: python -m chief record-demo", critical=must))
        return
    cache = DemoCache.load(path)
    if len(cache) == 0:
        out.append(Check("Demo cache", FAIL if must else quiet, f"{path} is empty or unreadable", "while online, run: python -m chief record-demo", critical=must))
        return
    age = cache.age(d.now())
    parts = ", ".join(f"{n} {t}" for t, n in sorted(cache.counts().items()))
    stale = age is not None and age > STALE_AFTER
    detail = f"{len(cache)} entries ({parts}); recorded {human_age(age) + ' ago' if age is not None else 'at an unknown time'}"
    out.append(Check("Demo cache", WARN if stale else OK, detail, "re-record shortly before the demo: python -m chief record-demo" if stale else ""))


# --------------------------------------------------------------------------- output


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking]


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{TAGS[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"       fix: {c.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append("All critical checks passed." if not blocking(checks) else f"{len(blocking(checks))} critical problem(s) must be fixed before the demo.")
    lines.append(f"({fails} failing, {warns} warning)")
    return "\n".join(lines)


def doctor_main(cfg: Config, *, live: bool = False, offline: bool = False, port: int = 8765, deps: Deps | None = None, out: Callable[[str], None] = print) -> int:
    checks = run_checks(cfg, deps, live=live, offline=offline, port=port)
    out(format_report(checks))
    return 1 if blocking(checks) else 0

"""`python -m chief demo`: one command to get ready for the stage.

  1. runs the doctor's critical checks and stops if any fail (--force to go on anyway)
  2. starts the web server (phone page + Stage) with an access token: CHIEF_WEB_TOKEN, else a random one
  3. starts a cloudflared quick tunnel if (and only if) it is already installed; shuts it down cleanly
  4. prints the Stage URL, the phone URL (plus a QR code if `qrcode` or `segno` happens to be installed)
     and the exact phrases to say

Nothing is installed or downloaded, and .env is never written. The phone URL contains the token: do not
put this terminal on the projector.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import threading
from typing import Any, Callable

from chief.config import Config
from chief.doctor import Deps, blocking, format_report, run_checks
from chief.tools.replay import STORY

TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CLOUDFLARED_HINT = "winget install --id Cloudflare.cloudflared   (then open a new terminal)"


def _say(text: str) -> None:
    print(text, flush=True)  # the tunnel link is printed from another thread: never sit in a buffer


def story_phrases() -> list[str]:
    """What to say, in order, as printed for the presenter."""
    return [phrase for phrase, _ in STORY]


def render_qr(url: str, out: Callable[[str], None] = _say, import_module: Callable[[str], Any] | None = None) -> bool:
    """Print a QR code if a tiny QR package is already installed. Returns False (and prints nothing) otherwise."""
    import importlib

    load = import_module or importlib.import_module
    try:
        qrcode = load("qrcode")
    except ImportError:
        qrcode = None
    if qrcode is not None:
        try:
            import io

            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            buf = io.StringIO()
            qr.print_ascii(out=buf, invert=True)
            out(buf.getvalue().rstrip("\n"))
            return True
        except Exception:
            return False
    try:
        segno = load("segno")
    except ImportError:
        return False
    try:
        import io

        buf = io.StringIO()
        segno.make(url).terminal(out=buf, compact=True)
        out(buf.getvalue().rstrip("\n"))
        return True
    except Exception:
        return False


class Tunnel:
    """A cloudflared quick tunnel as a child process. Never installs anything; stop() always cleans up."""

    def __init__(self, port: int, popen: Callable[..., Any] = subprocess.Popen, exe: str = "cloudflared"):
        self.port, self.popen, self.exe = port, popen, exe
        self.proc: Any = None
        self.url: str | None = None
        self.found = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_url: Callable[[str], None]) -> None:
        self.proc = self.popen(
            [self.exe, "tunnel", "--url", f"http://localhost:{self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        def watch() -> None:
            try:
                for line in self.proc.stdout:
                    m = TUNNEL_URL.search(line)
                    if m and not self.url:
                        self.url = m.group(0)
                        self.found.set()
                        on_url(self.url)
            except Exception:
                pass

        self._thread = threading.Thread(target=watch, name="chief-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)


def default_runtime(cfg: Config):
    from chief.runtime import Runtime

    rt = Runtime(cfg)
    if cfg.show_windows:
        from chief import screen

        screen.attach(rt)
    return rt


def run_demo(
    cfg: Config,
    *,
    port: int = 8765,
    host: str = "127.0.0.1",
    use_tunnel: bool = True,
    offline_check: bool = False,
    force: bool = False,
    deps: Deps | None = None,
    make_runtime: Callable[[Config], Any] = default_runtime,
    serve_fn: Callable[..., None] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    out: Callable[[str], None] = _say,
    qr: Callable[..., bool] = render_qr,
) -> int:
    d = deps or Deps()
    checks = run_checks(cfg, d, offline=offline_check, port=port)
    bad = blocking(checks)
    if bad:
        out(format_report([c for c in checks if c.status in ("fail", "warn")]))
        if not force:
            out("\nNot starting: fix the critical problems above (or run with --force to start anyway).")
            return 1
        out("\n--force given: starting anyway.")
    else:
        warns = [c for c in checks if c.status == "warn"]
        out(f"Preflight: all critical checks passed ({len(warns)} warning(s); run `python -m chief doctor` for details).")
        for c in warns:
            out(f"  warning: {c.name}: {c.detail}")

    token = os.getenv("CHIEF_WEB_TOKEN") or secrets.token_urlsafe(16)
    generated = not os.getenv("CHIEF_WEB_TOKEN")
    cfg_mode_note = {
        "mock": "MOCK: seeded data, nothing real is read or written",
        "live": "LIVE: real Gmail, Calendar and web search, no safety net",
        "demo": "DEMO: live, with cached answers if a read fails (tagged 'cached' on the Stage); writes never faked",
        "demo-offline": "OFFLINE: cached reads, writes are SIMULATED and tagged 'simulated'; nothing real is touched",
    }.get(cfg.mode, cfg.mode)

    if cfg.mode == "demo-offline" and cfg.llm_api_key:
        cfg.llm_api_key = None  # no network: skip the model so no call can hang; summaries use their built-in wording
        out("Offline mode: the language model is switched off for this run (answers use Chief's built-in wording).")

    try:
        rt = make_runtime(cfg)
    except Exception as exc:
        out(f"Could not start Chief: {type(exc).__name__}: {exc}")
        return 1

    if serve_fn is None:
        try:
            from chief.web.app import serve as serve_fn  # noqa: PLW0127
        except ImportError:
            out('The web page needs the live extras: pip install -e ".[live]"')
            return 1

    stage = f"http://localhost:{port}/stage?token={token}"
    local_phone = f"http://localhost:{port}/?token={token}"
    out("")
    out(f"Chief demo, {cfg_mode_note}")
    out(f"  Stage (projector, this laptop): {stage}")
    out(f"  Phone page on this laptop:      {local_phone}")
    if generated:
        out("  (CHIEF_WEB_TOKEN is not set, so this token is random for this run; the links change on every start.)")

    tunnel: Tunnel | None = None

    def show_phone(base: str) -> None:
        url = f"{base}/?token={token}"
        out(f"\n  PHONE (open this on the phone): {url}")
        if not qr(url, out):
            out("  (no QR package installed; type or paste the link. `pip install qrcode` is optional.)")
        out("  Keep this terminal off the projector: the link is a password.\n")

    if use_tunnel:
        exe = d.which("cloudflared")
        if exe:
            tunnel = Tunnel(port, popen=popen, exe=exe)
            try:
                tunnel.start(show_phone)
                out("  Tunnel: starting cloudflared, the phone link appears below in a few seconds...")
            except Exception as exc:
                out(f"  Tunnel failed to start ({type(exc).__name__}: {exc}). The phone page needs HTTPS for the mic.")
                tunnel = None
        else:
            out(f"  Tunnel: cloudflared is not installed, so the phone cannot use its mic. Install: {CLOUDFLARED_HINT}")
    else:
        out("  Tunnel: off (--no-tunnel). The phone mic needs an HTTPS link.")

    out("\nSay, in this order:")
    for i, phrase in enumerate(story_phrases(), 1):
        out(f"  {i}. {phrase}")
    if cfg.mode == "demo":
        out("\nIf the Wi-Fi drops: keep going; reads fall back to the cache and are tagged 'cached', writes fail honestly. "
            "To carry on with simulated writes: restart with  python -m chief demo --mode demo-offline --no-tunnel")
    elif cfg.mode == "live":
        out("\nLive mode has no safety net. For one, use --mode demo (run python -m chief record-demo first).")
    out("Ctrl+C stops everything.\n")

    try:
        serve_fn(rt, token, host, port)
        return 0
    except KeyboardInterrupt:
        out("\nStopping.")
        return 0
    finally:
        if tunnel is not None:
            tunnel.stop()
        closer = getattr(getattr(rt, "inner_tools", None), "close", None)
        if callable(closer):
            closer()


def record_demo_main(
    cfg: Config,
    *,
    dry_run: bool = False,
    tools: Any = None,
    out: Callable[[str], None] = _say,
) -> int:
    """`python -m chief record-demo`: run the story through the LIVE tools to fill the demo cache.

    Reads only. Every write (drafts, sends, calendar changes) is refused while recording, and the approval
    phrases are not run, so this never changes your accounts. `--dry-run` prints the plan and calls nothing."""
    from chief.tools.replay import STORY, DemoCache, record_story

    path = cfg.cache_path
    out(f"Recording the demo story through the live tools into {path}")
    for i, (phrase, runs) in enumerate(STORY, 1):
        out(f"  {i}. {phrase!r}  {'(runs read tools)' if runs else '(approval step: skipped while recording)'}")
    if dry_run:
        out("\nDry run: nothing was called and nothing was written. Needs COMPOSIO_API_KEY and PARALLEL_API_KEY; "
            "writes are never run while recording.")
        return 0

    missing = [n for n in ("COMPOSIO_API_KEY", "PARALLEL_API_KEY") if not os.getenv(n, "").strip()]
    if tools is None and missing:
        out(f"\nCannot record: {', '.join(missing)} not set. Add them to .env, then run `python -m chief doctor`.")
        return 1
    if tools is None:
        from chief.tools.live import LiveTools

        live_cfg = Config(**{**cfg.__dict__, "mode": "live"})
        tools = LiveTools(live_cfg)
    cache = DemoCache(path)
    out("")
    summary = record_story(cfg if cfg.mode == "live" else Config(**{**cfg.__dict__, "mode": "live"}), tools, cache, say=out)
    if summary["entries"] == 0:
        out("\nNothing was recorded, so the existing cache (if any) was left alone. Errors:")
        for e in summary["errors"]:
            out(f"  - {e}")
        return 1
    if path.exists():
        path.replace(path.with_suffix(path.suffix + ".bak"))  # keep the last good cache until this one is proven
        out(f"(previous cache kept as {path.name}.bak)")
    cache.save()
    out(f"\nSaved {summary['entries']} cached read(s) to {path}.")
    if summary["errors"]:
        out(f"{len(summary['errors'])} call(s) failed while recording (those parts of the story have no safety net):")
        for e in summary["errors"][:8]:
            out(f"  - {e}")
        return 1
    return 0

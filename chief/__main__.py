"""CLI:  python -m chief run --once [--approve]   |   web [--port N]   |   skills   |   reset   |   show-login   |   show-demo   |   doctor   |   demo   |   record-demo"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import sys
from datetime import datetime

from chief.config import Config
from chief.heartbeat import run_forever
from chief.runtime import Runtime


def _serve_web(rt: Runtime, host: str, port: int) -> int:
    try:
        from chief.web.app import serve
    except ImportError:
        print('The web page needs the live extras: pip install -e ".[live]"')
        return 1
    token = os.getenv("CHIEF_WEB_TOKEN") or secrets.token_urlsafe(16)
    print(f"Chief voice page ({rt.cfg.mode} mode): http://localhost:{port}/?token={token}")
    if not os.getenv("CHIEF_WEB_TOKEN"):
        print("(CHIEF_WEB_TOKEN is not set, so that token is random for this run.)")
    serve(rt, token, host, port)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chief")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the heartbeat")
    run.add_argument("--once", action="store_true", help="one tick, then exit")
    run.add_argument("--approve", action="store_true", help="approve every proposal (for demos and tests)")
    run.add_argument("--now", help="pin the current time, ISO format")
    run.add_argument("--show-windows", action="store_true", help="open the real Gmail/Calendar/source pages as Chief works")
    web = sub.add_parser("web", help="serve the voice page (phone push-to-talk)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--host", default="127.0.0.1", help="bind address; the tunnel connects locally")
    web.add_argument("--show-windows", action="store_true", help="open the real Gmail/Calendar/source pages as Chief works")
    sub.add_parser("skills", help="list loaded skills")
    sub.add_parser("reset", help="forget past decisions and the activity log")
    sub.add_parser("slack", help="approve proposals from Slack (Socket Mode) and run the heartbeat")
    sub.add_parser("show-login", help="open the dedicated browser profile once to sign into the TEST Google account")
    sub.add_parser("show-layout", help="list your monitors and suggest where to put the real windows")
    demo = sub.add_parser("show-demo", help="replay canned events so you can watch the real windows open")
    demo.add_argument("--dry-run", action="store_true", help="print the URLs instead of opening a browser")
    doctor = sub.add_parser("doctor", help="preflight checklist for demo day (names of keys only, never values)")
    doctor.add_argument("--live", action="store_true", help="also make one tiny call each to Parallel, the LLM and speech-to-text")
    doctor.add_argument("--offline", action="store_true", help="skip every network check")
    doctor.add_argument("--port", type=int, default=8765)
    stage = sub.add_parser("demo", help="doctor, then the web server + tunnel + the phrases to say")
    stage.add_argument("--mode", choices=["mock", "live", "demo", "demo-offline"], default="demo", help="default: demo (live with cached failover)")
    stage.add_argument("--port", type=int, default=8765)
    stage.add_argument("--host", default="127.0.0.1")
    stage.add_argument("--no-tunnel", action="store_true", help="do not start cloudflared")
    stage.add_argument("--offline", action="store_true", help="skip network checks in the preflight")
    stage.add_argument("--force", action="store_true", help="start even if a critical check fails")
    rec = sub.add_parser("record-demo", help="run the story through the live tools to fill the demo cache (reads only)")
    rec.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if args.cmd == "doctor":
        from chief.doctor import doctor_main

        return doctor_main(cfg, live=args.live, offline=args.offline, port=args.port)
    if args.cmd == "demo":
        from chief.launcher import run_demo

        cfg.mode = args.mode
        return run_demo(cfg, port=args.port, host=args.host, use_tunnel=not args.no_tunnel, offline_check=args.offline or args.mode in ("mock", "demo-offline"), force=args.force)
    if args.cmd == "record-demo":
        from chief.launcher import record_demo_main

        return record_demo_main(cfg, dry_run=args.dry_run)
    if args.cmd == "reset":
        shutil.rmtree(cfg.state_dir, ignore_errors=True)
        print(f"cleared {cfg.state_dir}")
        return 0

    if args.cmd in ("show-login", "show-demo", "show-layout"):
        from chief import screen

        if getattr(args, "dry_run", False):
            cfg.show_dry_run = True
        if args.cmd == "show-layout":
            return screen.show_layout(cfg)
        return screen.show_login(cfg) if args.cmd == "show-login" else screen.show_demo(cfg)

    if args.cmd == "slack":
        from chief.slack_app import run_slack

        return run_slack(cfg)

    if getattr(args, "now", None):
        cfg.now = datetime.fromisoformat(args.now)
    rt = Runtime(cfg)
    if getattr(args, "show_windows", False) or cfg.show_windows:
        from chief import screen

        screen.attach(rt)

    if args.cmd == "web":
        return _serve_web(rt, args.host, args.port)

    if args.cmd == "skills":
        for s in rt.skills.values():
            print(f"{s.name:14} {s.description}")
        return 0

    if not args.once:
        print(f"Chief ({cfg.mode} mode): checking in every {cfg.heartbeat_hours:g}h. Ctrl+C to stop.")
        asyncio.run(run_forever(rt))
        return 0

    proposals = rt.tick()
    if not proposals:
        print("Nothing worth your attention. (NO_REPLY)")
        return 0
    for p in proposals:
        if args.approve:
            results = rt.gate.approve(p.id)
            print(f"\napproved {p.id}: {len(results)} action(s) ran")
        else:
            try:
                choice = input("approve this? [y/N] ").strip().lower() if sys.stdin.isatty() else None
            except EOFError:
                choice = None
            if choice is None:
                print("\n(pending: run again with --approve to apply)")
            elif choice == "y":
                rt.gate.approve(p.id)
                print("done.")
            else:
                rt.gate.skip(p.id)
                print("skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

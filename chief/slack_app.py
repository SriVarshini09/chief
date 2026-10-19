"""Slack approval surface (Socket Mode: no public URL needed).

Chief posts each Proposal as a Block Kit card with Approve / Skip buttons. A click is plain code
(no LLM): ack within Slack's 3 seconds, hand the id to the approval gate, then rewrite the card to
show what happened.

`slack_bolt` is imported only inside `build_app`, so everything else here (block rendering, the
click handlers, `notify`) runs and is tested without the package or a network.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from chief.config import Config
from chief.models import Proposal

log = logging.getLogger("chief.slack")

APPROVE, SKIP = "approve", "skip"
_TEXT_LIMIT = 2900  # Slack caps a section's text at 3000 characters


def _esc(text: str) -> str:
    """Escape the characters Slack treats as control sequences in mrkdwn."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int = _TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _body_blocks(p: Proposal) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*[{_esc(p.skill)}]* {_esc(p.summary)}")}}
    ]
    if p.actions:
        lines = [f"• `{_esc(a.describe()).replace('`', chr(39))}`" for a in p.actions]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip("*Will do if approved:*\n" + "\n".join(lines))}})
    if p.sources:
        src = "\n".join(f"• {_esc(s)}" for s in p.sources)
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _clip("Sources:\n" + src)}]})
    return blocks


def fallback_text(p: Proposal) -> str:
    """Plain-text version for notifications and clients that cannot render blocks."""
    return f"Chief [{p.skill}]: {p.summary}"


def proposal_blocks(p: Proposal) -> list[dict]:
    """The proposal card: summary, actions, sources, and Approve / Skip buttons (value = proposal id)."""
    buttons = {
        "type": "actions",
        "block_id": f"chief_{p.id}",
        "elements": [
            {"type": "button", "action_id": APPROVE, "style": "primary", "value": p.id,
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": SKIP, "value": p.id,
             "text": {"type": "plain_text", "text": "Skip"}},
        ],
    }
    return _body_blocks(p) + [buttons]


def outcome_blocks(p: Proposal, status: str) -> list[dict]:
    """The same card with the buttons replaced by a one-line outcome."""
    return _body_blocks(p) + [{"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(status)}]}]


def _strip_buttons(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") != "actions"]


def make_notify(client: Any, channel: str) -> Callable[[Proposal], None]:
    """A `notify` for `Runtime(cfg, notify=...)`: posts the proposal card to the channel."""

    def notify(p: Proposal) -> None:
        client.chat_postMessage(channel=channel, text=fallback_text(p), blocks=proposal_blocks(p))

    return notify


def _results_status(results: list[dict]) -> str:
    errors = [r for r in results if "error" in r]
    if not errors:
        return f":white_check_mark: *Approved.* {len(results)} action(s) ran."
    failed = "; ".join(f"`{_esc(r['tool'])}`: {_esc(str(r['error']))}" for r in errors)
    ran = len(results) - len(errors)
    return f":warning: *Approved, but an action failed* ({ran} ran before it). {failed}\nNothing was recorded; Chief may propose it again."


def _update(client: Any, body: dict, blocks: list[dict], text: str) -> None:
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, blocks=blocks
    )


def _handle(runtime: Any, ack: Callable, body: dict, client: Any, decision: str) -> None:
    ack()  # first, always: Slack needs a response within 3 seconds
    try:
        proposal_id = body["actions"][0]["value"]
        proposal = runtime.gate.pending.get(proposal_id)
        if proposal is None:
            return _already_handled(client, body, proposal_id)
        try:
            if decision == APPROVE:
                status = _results_status(runtime.gate.approve(proposal_id))
            else:
                runtime.gate.skip(proposal_id)
                status = ":no_entry_sign: *Skipped.* Chief won't ask about this again."
        except KeyError:  # a second click raced us and the gate already popped it
            return _already_handled(client, body, proposal_id)
        _update(client, body, outcome_blocks(proposal, status), f"{fallback_text(proposal)} ({decision})")
    except Exception:
        log.exception("slack %s handler failed", decision)


def _already_handled(client: Any, body: dict, proposal_id: str) -> None:
    """Unknown or already-decided id (double click, or Chief restarted since the card was posted)."""
    note = {"type": "context", "elements": [{"type": "mrkdwn", "text": ":information_source: This proposal was already handled or has expired."}]}
    old = _strip_buttons(body.get("message", {}).get("blocks") or [])
    _update(client, body, old + [note], "Proposal already handled")
    log.info("click on unknown or handled proposal %s", proposal_id)


def register_handlers(app: Any, runtime: Any) -> None:
    """Attach the Approve / Skip button handlers to a bolt App (or anything with `.action`)."""

    @app.action(APPROVE)
    def on_approve(ack, body, client):
        _handle(runtime, ack, body, client, APPROVE)

    @app.action(SKIP)
    def on_skip(ack, body, client):
        _handle(runtime, ack, body, client, SKIP)


def build_app(cfg: Config) -> tuple[Any, Any]:
    """Create the bolt App and a Runtime whose proposals are posted to Slack. Returns (app, runtime)."""
    from slack_bolt import App  # lazy: only the `slack` subcommand needs the package

    from chief.runtime import Runtime

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    runtime = Runtime(cfg, notify=make_notify(app.client, os.environ["SLACK_CHANNEL"]))
    register_handlers(app, runtime)
    return app, runtime


def run_slack(cfg: Config) -> int:
    """`python -m chief slack`: start Socket Mode (button clicks) and the heartbeat (new proposals)."""
    missing = [v for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL") if not os.getenv(v)]
    if missing:
        print(f"Missing {', '.join(missing)}. See docs/slack-setup.md.")
        return 2
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("slack-bolt is not installed. Run: pip install -e .[live]")
        return 2
    from chief.heartbeat import run_forever

    app, rt = build_app(cfg)
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.connect()  # non-blocking: listens for clicks on its own thread
    print(f"Chief ({cfg.mode} mode) is on Slack, posting to {os.environ['SLACK_CHANNEL']}. "
          f"Checking in every {cfg.heartbeat_hours:g}h. Ctrl+C to stop.")
    try:
        asyncio.run(run_forever(rt))
    except KeyboardInterrupt:
        pass
    finally:
        handler.close()
    return 0

"""Telegram surface: Chief in your pocket, with notifications and no tunnel.

The bot long-polls Telegram, so it needs no public URL. One loop does everything, one thing at a time:
the heartbeat, the morning brief, then whatever you sent. Approve and Skip are plain code that hands a
proposal id to the approval gate; typed words go through the same intent mapper as the phone page.

Anyone can find a Telegram bot and message it, so everything from a sender other than
TELEGRAM_USER_ID is dropped. Bot tokens appear in API URLs, so errors here are logged by type only.

httpx is imported only inside `run_telegram`, so the rest runs and is tested with a fake API.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from chief.config import Config
from chief.gate import results_text
from chief.intent import handle_utterance
from chief.models import Proposal
from chief.runtime import Runtime

log = logging.getLogger("chief.telegram")

API_URL = "https://api.telegram.org"
APPROVE, SKIP, UNDO = "approve", "skip", "undo"
CHUNK = 4000  # Telegram caps a message at 4096 characters
POLL_SECONDS = 25
MAX_TEXT_CHARS = 2000
BRIEF_WINDOW_HOURS = 3  # started later than this after the brief hour, Chief skips that day's brief
HELP = (
    "Hi, I'm Chief. I change nothing until you approve it.\n\n"
    "Try:\n"
    "- What's in my inbox?\n"
    "- How's my week looking?\n"
    "- Reply to Sam and say Friday works\n"
    "- Morning brief"
)


class TelegramError(Exception):
    """Telegram refused a call. The message is Telegram's own text and never contains the token."""


class TelegramApi:
    def __init__(self, token: str, client: Any):
        self.base, self.client = f"{API_URL}/bot{token}", client

    def call(self, method: str, **params: Any) -> Any:
        wait = params.get("timeout", 0) + 10
        data = self.client.post(f"{self.base}/{method}", json=params, timeout=wait).json()
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or method))
        return data.get("result")


def card_text(p: Proposal) -> str:
    """Everything the person needs in order to decide, in plain words."""
    lines = [p.summary]
    if p.actions:
        lines += ["", "If you approve, I will:"] + [f"- {a.plain()}" for a in p.actions]
    if p.sources:
        lines += ["", "Sources:"] + [f"- {s}" for s in p.sources]
    return "\n".join(lines)


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split, never clip: an approval must not hide the end of the email it would send."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def buttons(proposal_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"{APPROVE}:{proposal_id}"},
        {"text": "Skip", "callback_data": f"{SKIP}:{proposal_id}"},
    ]]}


def brief_due(rt: Runtime, now: datetime) -> bool:
    hour = rt.cfg.brief_hour
    if hour is None or not hour <= now.hour < hour + BRIEF_WINDOW_HOURS:
        return False
    return not rt.memory.decided(f"brief_sent:{now.date()}")


def send_brief_if_due(rt: Runtime, now: datetime) -> bool:
    """At most once a day. If sending fails nothing is recorded, so the next loop tries again."""
    if not brief_due(rt, now):
        return False
    proposal = rt.run_skill("morning_brief", now)
    if proposal is None:
        return False
    rt.memory.record(key=f"brief_sent:{now.date()}", skill="morning_brief", decision="sent", summary=proposal.summary)
    return True


class Bot:
    def __init__(self, api: TelegramApi, owner: int):
        self.api, self.owner = api, owner
        self.rt: Runtime | None = None  # set by attach: Runtime needs `notify` before the bot can have it
        self.cards_sent = 0
        self.offset: int | None = None
        self.next_tick = 0.0

    def attach(self, rt: Runtime) -> "Bot":
        self.rt = rt
        return self

    def send(self, text: str, markup: dict | None = None) -> None:
        parts = chunks(text)
        for i, part in enumerate(parts):
            params: dict = {"chat_id": self.owner, "text": part, "link_preview_options": {"is_disabled": True}}
            if markup and i == len(parts) - 1:
                params["reply_markup"] = markup
            self.api.call("sendMessage", **params)

    def notify(self, p: Proposal) -> None:
        """The gate's notify: every proposal becomes a card, and a card with actions gets buttons."""
        self.cards_sent += 1
        self.send(card_text(p), buttons(p.id) if p.actions else None)

    def handle(self, update: dict) -> None:
        """One update from Telegram. Never raises: a bad update must not be retried forever."""
        try:
            click, message = update.get("callback_query"), update.get("message")
            if click and self._from_owner(click, click.get("message") or {}):
                self._on_click(click)
            elif message and self._from_owner(message, message):
                self._on_message(message)
        except Exception as exc:
            log.warning("telegram update failed: %s", type(exc).__name__)
            self._try_send("Sorry, something went wrong.")

    def _from_owner(self, sent: dict, message: dict) -> bool:
        sender = (sent.get("from") or {}).get("id")
        return sender == self.owner and (message.get("chat") or {}).get("id") == self.owner

    def _try_send(self, text: str) -> None:
        try:
            self.send(text)
        except Exception as exc:
            log.warning("telegram send failed: %s", type(exc).__name__)

    def _on_message(self, message: dict) -> None:
        text = (message.get("text") or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            return self.send("I can only read text here for now.")
        if text.split()[0] in ("/start", "/help"):
            return self.send(HELP)
        with self.rt.turn_lock:
            self.cards_sent = 0
            out = handle_utterance(self.rt, text)
            if not self.cards_sent:  # a card already said it
                self.send(out["say"], self._undo_button())

    def _undo_button(self) -> dict | None:
        state = self.rt.gate.undo_state()
        return {"inline_keyboard": [[{"text": state["label"], "callback_data": f"{UNDO}:{state['mark']}"}]]} if state else None

    def _on_click(self, click: dict) -> None:
        self.api.call("answerCallbackQuery", callback_query_id=click["id"])
        verb, _, proposal_id = str(click.get("data") or "").partition(":")
        if verb not in (APPROVE, SKIP, UNDO):
            return
        message = click.get("message") or {}
        self.api.call(
            "editMessageReplyMarkup", chat_id=self.owner, message_id=message.get("message_id"),
            reply_markup={"inline_keyboard": []},
        )
        with self.rt.turn_lock:
            if verb == UNDO:
                self.send(self.rt.gate.undo(proposal_id) or "That can no longer be undone.")
                return
            try:
                if verb == APPROVE:
                    self.send(results_text(self.rt.gate.approve(proposal_id)), self._undo_button())
                else:
                    self.rt.gate.skip(proposal_id)
                    self.send("Skipped. I won't ask about this again.")
            except KeyError:  # a double tap, or Chief restarted since the card was sent
                self.send("That one was already handled or has expired.")

    def scheduled(self) -> None:
        """The heartbeat and the morning brief. A failure waits for the next loop."""
        try:
            with self.rt.turn_lock:
                if time.monotonic() >= self.next_tick:
                    self.next_tick = time.monotonic() + self.rt.cfg.heartbeat_hours * 3600
                    self.rt.tick()
                send_brief_if_due(self.rt, self.rt.now())
        except Exception as exc:
            log.warning("telegram schedule failed: %s", type(exc).__name__)

    def poll(self) -> None:
        params: dict = {"timeout": POLL_SECONDS, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            params["offset"] = self.offset
        for update in self.api.call("getUpdates", **params):
            self.offset = update["update_id"] + 1  # moves on even if handling fails
            self.handle(update)

    def run(self) -> None:
        while True:
            self.scheduled()
            try:
                self.poll()
            except Exception as exc:
                log.warning("telegram poll failed: %s", type(exc).__name__)
                time.sleep(5)


def find_owner(api: TelegramApi) -> int:
    """First run: wait for one message, tell the sender their own ID, and stop."""
    print("TELEGRAM_USER_ID is not set. Send your bot any message from your Telegram account.")
    offset = None
    while True:
        params: dict = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for update in api.call("getUpdates", **params):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            sender = (message.get("from") or {}).get("id")
            if sender is None or (message.get("chat") or {}).get("type") != "private":
                continue
            api.call("sendMessage", chat_id=sender, text=f"Your Telegram ID is {sender}. Set TELEGRAM_USER_ID to it, then restart Chief.")
            print(f"Your ID is {sender}. Add TELEGRAM_USER_ID={sender} to .env and run this again.")
            return 0


def start_with_web(rt: Runtime) -> bool:
    """`chief web` with Telegram set up: both surfaces share one Chief, so a card shows on both."""
    token, owner = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_USER_ID", "").strip()
    if not token or not owner.isdigit():
        return False
    import httpx

    api = TelegramApi(token, httpx.Client())
    bot = Bot(api, int(owner)).attach(rt)
    earlier = rt.gate.notify

    def both(p: Proposal) -> None:
        earlier(p)
        try:
            bot.notify(p)
        except Exception as exc:  # the phone page must keep working if Telegram is unreachable
            log.warning("telegram notify failed: %s", type(exc).__name__)

    rt.gate.notify = both
    try:
        api.call("deleteWebhook", drop_pending_updates=True)
        bot.send(f"Chief is on ({rt.cfg.mode} mode).")
    except Exception as exc:
        print(f"Telegram is off: {type(exc).__name__}. Check TELEGRAM_BOT_TOKEN.")
        rt.gate.notify = earlier
        return False
    threading.Thread(target=bot.run, name="chief-telegram", daemon=True).start()
    return True


def run_telegram(cfg: Config) -> int:
    """`python -m chief telegram`: run Chief as a Telegram bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN. See docs/telegram-setup.md.")
        return 2
    try:
        import httpx
    except ImportError:
        print('Telegram needs the live extras: pip install -e ".[live]"')
        return 2
    owner = os.getenv("TELEGRAM_USER_ID", "").strip()
    if owner and not owner.isdigit():
        print("TELEGRAM_USER_ID must be your numeric ID. See docs/telegram-setup.md.")
        return 2
    api = TelegramApi(token, httpx.Client())
    try:
        if not owner:
            return find_owner(api)
        # Messages sent while Chief was off are dropped: an old "do it" must not approve something new.
        api.call("deleteWebhook", drop_pending_updates=True)
        bot = Bot(api, int(owner))
        bot.attach(Runtime(cfg, notify=bot.notify))
        bot.send(f"Chief is on ({cfg.mode} mode).")
        print(f"Chief ({cfg.mode} mode) is on Telegram. Ctrl+C to stop.")
        bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # by type only: the URL in an HTTP error holds the bot token
        print(f"Telegram failed: {type(exc).__name__}. Check TELEGRAM_BOT_TOKEN and your connection.")
        return 1
    return 0

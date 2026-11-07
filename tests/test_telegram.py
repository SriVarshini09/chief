"""The Telegram surface, against a fake API. No network, no token, no httpx needed."""

from __future__ import annotations

from datetime import datetime

import pytest

from chief.__main__ import main
from chief.models import Proposal, ToolCall
from chief.runtime import Runtime
from chief.telegram_app import Bot, TelegramApi, TelegramError, brief_due, buttons, card_text, chunks, send_brief_if_due, start_with_web
from chief.tools.base import WRITE_TOOLS

OWNER, STRANGER = 111, 999


class FakeApi:
    def __init__(self):
        self.calls, self.updates = [], []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        return {}

    def named(self, method):
        return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p["text"] for p in self.named("sendMessage")]


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def bot(api, cfg, tools):
    b = Bot(api, OWNER)
    return b.attach(Runtime(cfg, tools=tools, notify=b.notify))


def message(text, sender=OWNER, chat=None):
    return {"update_id": 1, "message": {"from": {"id": sender}, "chat": {"id": chat or sender, "type": "private"}, "text": text}}


def click(data, sender=OWNER):
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "from": {"id": sender}, "data": data,
        "message": {"message_id": 5, "chat": {"id": sender}},
    }}


def writes(tools):
    return [c for c in tools.calls if c[0] in WRITE_TOOLS]


def propose(bot):
    bot.handle(message("how's my week looking?"))
    return list(bot.rt.gate.pending)[-1]


def test_a_proposal_arrives_as_one_card_with_buttons(bot, api, tools):
    pid = propose(bot)
    sent = api.named("sendMessage")
    assert len(sent) == 1 and sent[0]["chat_id"] == OWNER
    assert sent[0]["reply_markup"] == buttons(pid)
    assert 'Mark "Vendor sync" as async' in sent[0]["text"]
    assert "patch_event" not in sent[0]["text"]
    assert writes(tools) == []  # nothing ran before approval


def test_approve_runs_through_the_gate_and_removes_the_buttons(bot, api, tools):
    pid = propose(bot)
    bot.handle(click(f"approve:{pid}"))
    assert len(writes(tools)) == 4 and pid not in bot.rt.gate.pending
    assert api.named("answerCallbackQuery") == [{"callback_query_id": "cb1"}]
    assert api.named("editMessageReplyMarkup")[0]["reply_markup"] == {"inline_keyboard": []}
    assert "Done. 4 change(s) made." in api.texts()


def test_skip_changes_nothing_and_is_remembered(bot, api, tools):
    pid = propose(bot)
    bot.handle(click(f"skip:{pid}"))
    assert writes(tools) == [] and bot.rt.memory.decided("audit:2026-10-22")
    assert any("Skipped" in t for t in api.texts())


def test_a_double_tap_runs_once(bot, api, tools):
    pid = propose(bot)
    bot.handle(click(f"approve:{pid}"))
    bot.handle(click(f"approve:{pid}"))
    assert len(writes(tools)) == 4
    assert any("already handled" in t for t in api.texts())


def test_a_typed_do_it_approves_the_latest(bot, api, tools):
    propose(bot)
    bot.handle(message("do it"))
    assert len(writes(tools)) == 4 and any("change(s) made" in t for t in api.texts())


def test_a_stranger_can_do_nothing(bot, api, tools):
    pid = propose(bot)
    before = len(api.calls)
    bot.handle(click(f"approve:{pid}", sender=STRANGER))
    bot.handle(message("do it", sender=STRANGER))
    bot.handle(message("what's in my inbox?", sender=STRANGER))
    assert writes(tools) == [] and pid in bot.rt.gate.pending
    assert len(api.calls) == before  # not even a reply


def test_the_owner_in_a_group_chat_is_ignored(bot, api):
    bot.handle(message("what's in my inbox?", chat=-500))
    assert api.calls == []


def test_a_click_with_an_unknown_verb_does_nothing(bot, tools):
    pid = propose(bot)
    bot.handle(click(f"delete:{pid}"))
    assert writes(tools) == [] and pid in bot.rt.gate.pending


def test_read_only_results_have_no_buttons_and_are_not_sent_twice(bot, api):
    bot.handle(message("what's in my inbox?"))
    sent = api.named("sendMessage")
    assert len(sent) == 1 and "reply_markup" not in sent[0] and sent[0]["text"].startswith("Inbox:")


def test_a_request_without_a_card_gets_the_spoken_reply(bot, api):
    bot.handle(message("tell me a joke"))
    assert any("I can help with" in t for t in api.texts())


def test_start_shows_help_and_a_photo_is_refused_politely(bot, api):
    bot.handle(message("/start"))
    bot.handle({"update_id": 3, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}}})
    assert "change nothing until you approve" in api.texts()[0]
    assert "read text and your shared location" in api.texts()[1]


def test_a_failure_while_handling_is_reported_not_raised(bot, api, monkeypatch):
    monkeypatch.setattr("chief.telegram_app.handle_utterance", lambda *a: 1 / 0)
    bot.handle(message("hello"))
    assert api.texts() == ["Sorry, something went wrong."]


def test_poll_moves_past_each_update(bot, api):
    api.updates = [{**message("/help"), "update_id": 40}]
    bot.poll()
    assert bot.offset == 41
    bot.poll()
    assert api.named("getUpdates")[-1]["offset"] == 41


def test_long_emails_are_split_and_never_clipped(bot, api):
    body = "word " * 2000
    p = Proposal("draft_reply", "Send it?", [ToolCall("send_email", {"to": "a@example.com", "subject": "s", "body": body})], key="k", id="p1")
    bot.notify(p)
    sent = api.named("sendMessage")
    assert len(sent) > 1 and all(len(s["text"]) <= 4096 for s in sent)
    assert "".join(s["text"] for s in sent) == card_text(p)  # the whole email is shown
    assert "reply_markup" in sent[-1] and not any("reply_markup" in s for s in sent[:-1])  # buttons after the full text


def test_messages_are_plain_text_with_no_link_previews(bot, api):
    bot.handle(message("what's in my inbox?"))
    sent = api.named("sendMessage")[0]
    assert "parse_mode" not in sent  # email text cannot inject formatting
    assert sent["link_preview_options"] == {"is_disabled": True}  # Telegram must not fetch links from mail


def test_chunks_of_empty_text():
    assert chunks("") == [""]


# ---- undo and working beside the phone page --------------------------------------------------------


def undo_button(sent):
    return sent["reply_markup"]["inline_keyboard"][0][0]


def test_an_approval_offers_an_undo_button_that_reverses_it(bot, api, tools):
    pid = propose(bot)
    before = {k: e.title for k, e in tools.events.items()}
    bot.handle(click(f"approve:{pid}"))
    done = next(s for s in api.named("sendMessage") if s["text"].startswith("Done"))
    assert undo_button(done)["text"] == "Undo"
    bot.handle(click(undo_button(done)["callback_data"]))
    assert {k: e.title for k, e in tools.events.items()} == before
    assert any(t.startswith("Undone") for t in api.texts())


def test_a_stale_undo_button_does_nothing(bot, api, tools):
    pid = propose(bot)
    bot.handle(click(f"approve:{pid}"))
    count = len(tools.calls)
    bot.handle(click("undo:c999"))
    assert len(tools.calls) == count and "That can no longer be undone." in api.texts()


def test_a_stranger_cannot_press_undo(bot, api, tools):
    pid = propose(bot)
    bot.handle(click(f"approve:{pid}"))
    count = len(tools.calls)
    bot.handle(click(f"undo:{bot.rt.gate.undo_state()['mark']}", sender=STRANGER))
    assert len(tools.calls) == count


def test_telegram_stays_off_without_both_settings(rt, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_USER_ID", "111")
    assert start_with_web(rt) is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USER_ID", "not-a-number")
    assert start_with_web(rt) is False


# ---- the morning brief ----------------------------------------------------------------------------


def test_the_brief_goes_out_once_a_day(bot, api):
    morning = datetime(2026, 10, 21, 8, 5)
    assert send_brief_if_due(bot.rt, morning)
    assert not send_brief_if_due(bot.rt, datetime(2026, 10, 21, 9, 5))
    assert len(api.texts()) == 1 and api.texts()[0].startswith("Good morning")
    assert send_brief_if_due(bot.rt, datetime(2026, 10, 22, 8, 5))  # and again tomorrow


def test_a_failed_send_is_retried(bot, api):
    morning = datetime(2026, 10, 21, 8, 5)
    real, api.call = api.call, lambda *a, **k: 1 / 0
    with pytest.raises(ZeroDivisionError):
        send_brief_if_due(bot.rt, morning)
    api.call = real
    assert brief_due(bot.rt, morning)


@pytest.mark.parametrize("hour,due", [(7, False), (8, True), (10, True), (11, False), (15, False)])
def test_the_brief_only_goes_out_in_the_morning(bot, hour, due):
    assert brief_due(bot.rt, datetime(2026, 10, 21, hour, 0)) is due


def test_the_brief_can_be_turned_off(bot):
    bot.rt.cfg.brief_hour = None
    assert not brief_due(bot.rt, datetime(2026, 10, 21, 8, 0))


def test_scheduled_runs_the_heartbeat_and_survives_a_failure(bot, api, monkeypatch):
    bot.scheduled()
    assert any("Mark" in t for t in api.texts())  # the audit proposal arrived unasked
    assert any(t.startswith("Good morning") for t in api.texts())
    monkeypatch.setattr("chief.telegram_app.send_brief_if_due", lambda *a: 1 / 0)
    bot.scheduled()  # no exception


# ---- the API wrapper and the command --------------------------------------------------------------


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeClient:
    def __init__(self, data):
        self.data, self.posts = data, []

    def post(self, url, json, timeout):
        self.posts.append((url, json, timeout))
        return FakeResponse(self.data)


def test_api_calls_the_bot_url_and_waits_past_the_long_poll():
    client = FakeClient({"ok": True, "result": [1]})
    assert TelegramApi("tok", client).call("getUpdates", timeout=25) == [1]
    url, params, timeout = client.posts[0]
    assert url.endswith("/bottok/getUpdates") and params == {"timeout": 25} and timeout > 25


def test_api_error_carries_telegrams_words_and_not_the_token():
    with pytest.raises(TelegramError) as err:
        TelegramApi("tok-secret", FakeClient({"ok": False, "description": "Unauthorized"})).call("getMe")
    assert str(err.value) == "Unauthorized"


def test_command_needs_a_token(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("CHIEF_STATE_DIR", str(tmp_path / "state"))
    assert main(["telegram"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out

"""Slack approval surface, tested with a fake Slack client and a fake bolt App (no network)."""

from __future__ import annotations

from chief import slack_app
from chief.__main__ import main
from chief.models import Proposal, ToolCall


class FakeClient:
    def __init__(self):
        self.posts, self.updates = [], []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)

    def chat_update(self, **kw):
        self.updates.append(kw)


class FakeApp:
    """Stands in for slack_bolt.App: records the handlers registered with @app.action(id)."""

    def __init__(self):
        self.handlers = {}

    def action(self, action_id):
        def deco(fn):
            self.handlers[action_id] = fn
            return fn

        return deco


def click(app, client, action_id, proposal_id, message_blocks=None):
    acked = []
    body = {
        "actions": [{"action_id": action_id, "value": proposal_id}],
        "channel": {"id": "C1"},
        "message": {"ts": "111.222", "blocks": message_blocks or []},
    }
    app.handlers[action_id](ack=lambda: acked.append(True), body=body, client=client)
    return acked


def buttons(blocks):
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_blocks_show_summary_actions_sources_and_buttons():
    p = Proposal("audit", "Move standup", [ToolCall("patch_event", {"id": "e1"})], key="k", sources=["calendar", "a<b>"], id="abc123")
    blocks = slack_app.proposal_blocks(p)
    text = " ".join(str(b) for b in blocks)
    assert "Move standup" in text and "patch_event(id=" in text
    assert "calendar" in text and "a&lt;b&gt;" in text  # sources shown, mrkdwn escaped
    approve, skip = buttons(blocks)
    assert (approve["action_id"], approve["value"]) == ("approve", "abc123")
    assert (skip["action_id"], skip["value"]) == ("skip", "abc123")


def test_blocks_without_sources_have_no_context_block():
    p = Proposal("audit", "s", [ToolCall("patch_event", {})], key="k")
    assert not [b for b in slack_app.proposal_blocks(p) if b["type"] == "context"]


def test_long_summary_is_clipped_to_slack_limit():
    p = Proposal("audit", "x" * 10_000, [], key="k")
    assert all(len(b["text"]["text"]) <= 3000 for b in slack_app.proposal_blocks(p) if b["type"] == "section")


def test_notify_posts_card_to_channel():
    client = FakeClient()
    p = Proposal("audit", "hello", [ToolCall("patch_event", {})], key="k", id="p1")
    slack_app.make_notify(client, "C99")(p)
    post = client.posts[0]
    assert post["channel"] == "C99" and "hello" in post["text"]
    assert buttons(post["blocks"])[0]["value"] == "p1"


def test_approve_click_acks_runs_actions_and_updates_message(rt, tools):
    client, app = FakeClient(), FakeApp()
    slack_app.register_handlers(app, rt)
    p = rt.run_skill("audit")
    acked = click(app, client, "approve", p.id)
    assert acked == [True]
    assert any(c[0] in ("patch_event", "create_event") for c in tools.calls)
    assert rt.memory.decided(p.key)
    upd = client.updates[0]
    assert (upd["channel"], upd["ts"]) == ("C1", "111.222")
    assert not buttons(upd["blocks"]) and "Approved" in str(upd["blocks"])


def test_approve_failure_is_shown_on_the_card(rt, tools, monkeypatch):
    client, app = FakeClient(), FakeApp()
    slack_app.register_handlers(app, rt)
    p = rt.run_skill("audit")
    monkeypatch.setattr(tools, "patch_event", lambda **_: (_ for _ in ()).throw(RuntimeError("api down")))
    monkeypatch.setattr(tools, "create_event", lambda **_: (_ for _ in ()).throw(RuntimeError("api down")))
    click(app, client, "approve", p.id)
    shown = str(client.updates[0]["blocks"])
    assert "failed" in shown and "api down" in shown
    assert not rt.memory.decided(p.key)


def test_skip_click_runs_nothing_and_updates_message(rt, tools):
    client, app = FakeClient(), FakeApp()
    slack_app.register_handlers(app, rt)
    p = rt.run_skill("audit")
    assert click(app, client, "skip", p.id) == [True]
    assert tools.calls == [] and rt.memory.decided(p.key)
    assert "Skipped" in str(client.updates[0]["blocks"])


def test_unknown_id_is_acked_and_message_updated_without_error(rt, tools):
    client, app = FakeClient(), FakeApp()
    slack_app.register_handlers(app, rt)
    old = slack_app.proposal_blocks(Proposal("audit", "old", [], key="k", id="gone"))
    assert click(app, client, "approve", "gone", old) == [True]
    blocks = client.updates[0]["blocks"]
    assert not buttons(blocks) and "already handled" in str(blocks)
    assert tools.calls == []


def test_double_click_only_runs_once(rt, tools):
    client, app = FakeClient(), FakeApp()
    slack_app.register_handlers(app, rt)
    p = rt.run_skill("audit")
    click(app, client, "approve", p.id)
    n = len(tools.calls)
    click(app, client, "approve", p.id)
    assert len(tools.calls) == n
    assert "already handled" in str(client.updates[1]["blocks"])


def test_handler_survives_a_slack_api_error(rt):
    class Boom(FakeClient):
        def chat_update(self, **kw):
            raise RuntimeError("slack down")

    app = FakeApp()
    slack_app.register_handlers(app, rt)
    p = rt.run_skill("audit")
    assert click(app, Boom(), "skip", p.id) == [True]  # acked, no exception escapes


def test_slack_subcommand_reports_missing_tokens(monkeypatch, capsys):
    for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        monkeypatch.delenv(v, raising=False)
    assert main(["slack"]) == 2
    assert "SLACK_BOT_TOKEN" in capsys.readouterr().out

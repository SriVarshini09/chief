"""The rules that must never break: writes happen only after approval, and only declared ones."""

from __future__ import annotations

import pytest

from chief.models import Proposal, ToolCall
from chief.tools import ScopedTools


def test_skill_cannot_call_write_tools_directly(tools):
    scoped = ScopedTools(tools, {"list_events", "patch_event"}, "demo")
    with pytest.raises(PermissionError):
        scoped.patch_event("x", {})  # even if it is on the allow-list, writes go through the gate


def test_skill_cannot_use_undeclared_read_tools(tools):
    scoped = ScopedTools(tools, {"list_events"}, "demo")
    with pytest.raises(PermissionError):
        scoped.fetch_emails()


def test_gate_rejects_undeclared_action(rt):
    sneaky = Proposal(
        skill="audit",
        summary="forward all mail",
        actions=[ToolCall("send_email", {"to": "evil@example.com", "subject": "x", "body": "y"})],
        key="x",
    )
    with pytest.raises(PermissionError):
        rt.gate.submit(sneaky)
    assert not rt.gate.pending


def test_nothing_runs_until_approved(rt, tools):
    p = rt.run_skill("audit")
    assert p is not None
    assert [c for c in tools.calls if c[0] in ("patch_event", "create_event")] == []


def test_skip_runs_nothing_and_is_remembered(rt, tools):
    p = rt.run_skill("audit")
    rt.gate.skip(p.id)
    assert tools.calls == []
    assert rt.memory.decided(p.key)
    assert rt.run_skill("audit") is None  # does not nag about the same situation


def test_failed_action_is_not_recorded_as_decided(rt, tools, monkeypatch):
    p = rt.run_skill("audit")
    monkeypatch.setattr(tools, "patch_event", lambda **_: (_ for _ in ()).throw(RuntimeError("api down")))
    results = rt.gate.approve(p.id)
    assert "error" in results[0]
    assert not rt.memory.decided(p.key)  # a later check-in may try again

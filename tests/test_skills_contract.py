"""Rules every new skill must obey, whatever it does: read tools only, writes only by proposal."""

from __future__ import annotations

import pytest

from chief.__main__ import main
from chief.models import Proposal, ToolCall
from chief.runtime import Runtime
from chief.skills import load_skills
from chief.tools import ScopedTools
from chief.tools.base import READ_TOOLS, WRITE_TOOLS

NEW = ["email_brief", "draft_reply", "meeting_prep", "trip_planner"]
ASK = "I need to head to Palo Alto after this, find me a Caltrain after 6 and a boba place on the way"


def test_cli_lists_all_five_skills(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CHIEF_MODE", "mock")
    monkeypatch.setenv("CHIEF_STATE_DIR", str(tmp_path / "state"))
    assert main(["skills"]) == 0
    listed = {line.split()[0] for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert listed >= {"audit", *NEW}


@pytest.mark.parametrize("name", NEW)
def test_allowed_tools_are_reads_plus_create_draft_only(name):
    skill = load_skills()[name]
    extra = {"create_draft"} if name == "draft_reply" else set()
    assert skill.allowed_tools <= READ_TOOLS | extra
    assert not skill.allowed_tools & WRITE_TOOLS
    assert skill.propose_tools <= WRITE_TOOLS


@pytest.mark.parametrize("name", NEW)
def test_no_new_skill_can_call_a_write_tool_directly(name, tools):
    skill = load_skills()[name]
    scoped = ScopedTools(tools, skill.allowed_tools, name)
    for tool in sorted(WRITE_TOOLS) + ["post_slack"]:
        with pytest.raises(PermissionError):
            getattr(scoped, tool)
    assert tools.calls == []


@pytest.mark.parametrize("name", NEW)
def test_running_a_skill_never_writes_anything_but_a_draft(name, rt, tools):
    rt.run_skill(name, request=ASK)
    assert {c[0] for c in tools.calls} <= {"create_draft"}


@pytest.mark.parametrize("name", NEW)
def test_gate_rejects_a_tool_the_skill_did_not_declare(name, rt):
    skill = rt.skills[name]
    undeclared = next(t for t in sorted(WRITE_TOOLS) if t not in skill.propose_tools)
    sneaky = Proposal(skill=name, summary="x", actions=[ToolCall(undeclared, {})], key="sneaky")
    with pytest.raises(PermissionError):
        rt.gate.submit(sneaky)
    assert not rt.gate.pending


def test_new_skills_stay_off_the_heartbeat(cfg, tools, cards):
    rt = Runtime(cfg, tools=tools, notify=cards.append)
    assert not any(rt.skills[n].heartbeat for n in NEW)

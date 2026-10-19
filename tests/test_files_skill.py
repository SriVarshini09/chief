"""files skill: deterministic name resolution, refusals that say what can be opened, read-only results."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from chief.config import Config
from chief.intent import handle_utterance
from chief.runtime import Runtime
from chief.skills import files
from chief.tools import local
from chief.tools.mock import MockTools

NOW = datetime(2026, 10, 21, 8, 0)


@pytest.fixture(autouse=True)
def no_real_opening(monkeypatch):
    def trap(*a, **k):
        raise AssertionError("a test tried to open something for real")

    monkeypatch.setattr(local, "_default_launcher", trap)
    monkeypatch.setattr(os, "startfile", trap, raising=False)
    monkeypatch.delenv("CHIEF_LOCAL_ROOTS", raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Stand-ins for the four default folders, wired in where the tools look for them."""
    dirs = {n: tmp_path / n.title() for n in ("desktop", "documents", "downloads", "demo")}
    for d in dirs.values():
        d.mkdir()
    (dirs["desktop"] / "Pitch deck.pptx").write_text("x", encoding="utf-8")
    (dirs["desktop"] / "Notes").mkdir()
    (dirs["documents"] / "Acme").mkdir()
    (dirs["documents"] / "Q3 Plan").mkdir()
    (dirs["documents"] / "q3-plan").mkdir()
    for i in range(8):
        (dirs["downloads"] / f"file{i}.pdf").write_text("x", encoding="utf-8")
    monkeypatch.setattr(local, "default_folders", lambda home=None: {n: [d] for n, d in dirs.items()})
    # Only the demo folder is allowed by default (privacy); the rest are opt-in, exactly as a user would do it.
    monkeypatch.setenv("CHIEF_LOCAL_ROOTS", ";".join(str(d) for d in dirs.values()))
    return dirs


@pytest.fixture
def tools(tmp_path):
    t = MockTools(NOW)
    t.state_dir = tmp_path / "state"
    return t


@pytest.fixture
def rt(tools, tmp_path):
    cards = []
    r = Runtime(Config(mode="mock", state_dir=tmp_path / "state", now=NOW), tools=tools, notify=cards.append)
    r.cards = cards
    return r


def opened(tools):
    return [c[1]["path"] for c in tools.calls if c[0] == "open_folder"]


def test_a_real_open_says_it_opened_on_screen(rt, tools, home, monkeypatch):
    """When the tool really launched the folder (live mode) the answer says so; only mock mode hedges."""
    real = tools.open_folder
    monkeypatch.setattr(tools, "open_folder", lambda path: {k: v for k, v in real(path).items() if k != "simulated"})
    out = handle_utterance(rt, "open my desktop")
    assert out["say"].startswith("Opened Desktop on your screen.") and "Mock mode" not in out["say"]


@pytest.mark.parametrize("said,folder", [
    ("open my desktop", "desktop"),
    ("open the demo folder", "demo"),
    ("Open my demo folder, please!", "demo"),
    ("show me my documents", "documents"),
    ("show me my downloads", "downloads"),
    ("pull up the downloads folder", "downloads"),
])
def test_spoken_names_open_the_right_folder(said, folder, rt, tools, home):
    out = handle_utterance(rt, said)
    assert out["proposal"].skill == "files" and out["proposal"].actions == []
    assert opened(tools) == [str(home[folder].resolve())]
    # mock mode launches nothing, so it must not claim it did
    assert "Mock mode: nothing was really opened" in out["say"] and "on your screen" not in out["say"]


def test_asking_what_is_in_a_folder_lists_without_opening(rt, tools, home):
    out = handle_utterance(rt, "what's in my downloads")
    assert opened(tools) == [] and tools.calls == []
    assert "Downloads holds 8 item(s) (0 folder(s), 8 file(s))" in out["say"] and "and 3 more" in out["say"]
    assert 'Say "open downloads"' in out["say"]


def test_summary_is_short_and_read_only(rt, tools, home):
    out = handle_utterance(rt, "open my desktop")
    assert out["say"] == "(Mock mode: nothing was really opened.) Desktop: 2 item(s) (1 folder(s), 1 file(s)): Notes/, Pitch deck.pptx."
    assert out["proposal"].actions == [] and not rt.gate.pending
    assert {c[0] for c in tools.calls} == {"open_folder"}


def test_empty_folder_is_said_plainly(rt, tools, home):
    assert "It is empty" in handle_utterance(rt, "open the demo folder")["say"]


def test_missing_folder_is_reported_not_raised(rt, tools, home):
    home["demo"].rmdir()
    out = handle_utterance(rt, "open the demo folder")
    assert "couldn't open the demo folder" in out["say"] and opened(tools) == []


@pytest.mark.parametrize("said", ["open my music", "open the windows folder", "open ..\\..\\windows", "show me my secrets", "open my c drive"])
def test_unknown_names_are_refused_with_the_menu(said, rt, tools, home):
    skill_run = rt.run_skill("files", request=said)
    assert opened(tools) == [] and skill_run.actions == []
    assert "I can open: briefs, demo, desktop, documents, downloads." in skill_run.summary
    assert "don't know a folder called" in skill_run.summary


@pytest.mark.parametrize("said", ["open the folder", "open my folder", "open folder", "open a folder"])
def test_no_name_asks_which_one(said, rt, tools, home):
    out = handle_utterance(rt, said)
    assert out["say"].startswith("Which folder?") and "desktop" in out["say"] and opened(tools) == []


def test_a_sub_folder_is_matched_by_exact_name(rt, tools, home):
    p = rt.run_skill("files", request="open acme in my documents")
    assert opened(tools) == [str((home["documents"] / "Acme").resolve())]
    assert "Acme in Documents" in p.summary


def test_missing_sub_folder_lists_what_exists(rt, tools, home):
    p = rt.run_skill("files", request="open globex in my documents")
    assert opened(tools) == [] and 'no folder called "globex" in Documents' in p.summary and "Acme" in p.summary


def test_ambiguous_sub_folder_is_refused(rt, tools, home):
    p = rt.run_skill("files", request="open q3 plan in my documents")
    assert opened(tools) == [] and "matches 2 folders" in p.summary


def test_names_come_from_custom_roots_and_clashes_are_ambiguous(tmp_path, monkeypatch, rt, tools):
    a, b, c = tmp_path / "one" / "Work", tmp_path / "two" / "Work", tmp_path / "Music"
    for d in (a, b, c):
        d.mkdir(parents=True)
    monkeypatch.setenv("CHIEF_LOCAL_ROOTS", f"{a};{b};{c}")
    clash = rt.run_skill("files", request="open my work folder")
    assert opened(tools) == [] and "could mean more than one folder" in clash.summary
    ok = rt.run_skill("files", request="show me my music")
    assert opened(tools) == [str(c.resolve())]
    assert "I can open" not in ok.summary
    refused = rt.run_skill("files", request="open my desktop")  # not a root any more
    assert "don't know a folder called \"desktop\"" in refused.summary and "music" in refused.summary


def test_the_tools_still_refuse_when_the_skill_is_fooled(rt, tools, home, tmp_path, monkeypatch):
    """Even if name resolution pointed somewhere bad, open_folder re-checks the allow-list."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(files, "named_folders", lambda **kw: {"desktop": [elsewhere]})
    p = rt.run_skill("files", request="open my desktop")
    assert "outside the folders I may open" in p.summary and opened(tools) == []


def test_folder_names_with_control_characters_are_cleaned():
    assert files._clean("a\x1b[31mb\x00c") == "a[31mbc"


def test_triggers_route_here_and_not_to_open_brief(rt, home):
    for said in ("open my desktop", "what's in my downloads", "open the briefs folder", "show me the demo folder"):
        assert handle_utterance(rt, said)["proposal"].skill == "files", said
    for said in ("show me the brief", "open the brief"):
        assert handle_utterance(rt, said)["proposal"].skill == "open_brief", said


def test_empty_request_stays_silent(rt, home):
    assert rt.run_skill("files", request="") is None

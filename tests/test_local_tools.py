"""Safe local actions: allow-list, traversal and link tricks, extension block-list, mock vs live, scoping.

Nothing here opens a real window: every LocalActions gets a fake launcher, and the default launcher is
replaced with a trap wherever a test could reach it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from chief.activity import ActivityBus
from chief.config import Config
from chief.gate import ApprovalGate
from chief.memory import Memory
from chief.models import Proposal, ToolCall
from chief.skills import load_skills
from chief.tools import ALL_TOOLS, READ_TOOLS, SAFE_WRITE_TOOLS, WRITE_TOOLS, ScopedTools, Tools
from chief.tools import local
from chief.tools.live import LiveTools
from chief.tools.local import BLOCKED_EXTENSIONS, SAFE_EXTENSIONS, LocalActionError, LocalActions
from chief.tools.mock import MockTools
from chief.tools.recording import RecordingTools

NOW = datetime(2026, 10, 21, 8, 0)
WIN = os.name == "nt"
REAL_LAUNCHER = local._default_launcher  # captured before the trap fixture replaces it


@pytest.fixture(autouse=True)
def no_real_opening(monkeypatch):
    """Whatever a test does, the real opener must never run."""
    def trap(*a, **k):
        raise AssertionError("a test tried to open something for real")

    monkeypatch.setattr(local, "_default_launcher", trap)
    monkeypatch.setattr(os, "startfile", trap, raising=False)
    monkeypatch.delenv("CHIEF_LOCAL_ROOTS", raising=False)


@pytest.fixture
def world(tmp_path):
    safe, outside = tmp_path / "safe", tmp_path / "outside"
    for d in (safe / "Acme", outside):
        d.mkdir(parents=True)
    (safe / "notes.txt").write_text("hi", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    opened: list[str] = []
    return type("W", (), {
        "safe": safe, "outside": outside, "opened": opened, "tmp": tmp_path,
        "acts": LocalActions([safe], state_dir=tmp_path / "state", launcher=opened.append),
    })


def junction(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if not WIN:
            pytest.skip("cannot create links here")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if r.returncode:
            pytest.skip("cannot create a junction here")


# ------------------------------------------------------------------------------------------- contract
def test_tool_classes():
    assert "list_folder" in READ_TOOLS
    assert {"open_folder", "open_file"} <= SAFE_WRITE_TOOLS
    assert not {"list_folder", "open_folder", "open_file"} & WRITE_TOOLS
    assert {"list_folder", "open_folder", "open_file"} <= ALL_TOOLS


def test_base_tools_fail_loudly():
    for name in ("list_folder", "open_folder", "open_file"):
        with pytest.raises(NotImplementedError):
            getattr(Tools(), name)("C:/x")


def test_extension_lists():
    assert SAFE_EXTENSIONS == {"pdf", "png", "jpg", "jpeg", "gif", "txt", "md", "csv", "html", "docx", "xlsx", "pptx"}
    assert not SAFE_EXTENSIONS & BLOCKED_EXTENSIONS
    assert {"exe", "bat", "cmd", "ps1", "msi", "lnk", "js", "vbs", "py", "sh", "dll", "jar", "docm"} <= BLOCKED_EXTENSIONS


# -------------------------------------------------------------------------------------------- allow-list
def test_inside_the_root_is_fine(world):
    assert world.acts.list_folder(str(world.safe))["count"] == 2
    assert world.acts.open_folder(str(world.safe / "Acme"))["kind"] == "folder"
    assert world.opened == [str((world.safe / "Acme").resolve())]


def test_outside_the_root_is_refused_for_all_three(world):
    for fn, target in ((world.acts.list_folder, world.outside), (world.acts.open_folder, world.outside),
                       (world.acts.open_file, world.outside / "secret.txt")):
        with pytest.raises(LocalActionError, match="outside"):
            fn(str(target))
    assert world.opened == []


@pytest.mark.parametrize("make", [
    lambda w: str(w.safe / ".." / "outside"),
    lambda w: str(w.safe / "Acme" / ".." / ".." / "outside" / "secret.txt"),
    lambda w: str(w.safe) + "/../outside",
    lambda w: str(w.safe) + "-evil",  # shares the prefix, is a different folder
    lambda w: str(w.safe.parent),
    lambda w: str(w.safe / ".." / ".."),
])
def test_traversal_and_prefix_tricks(world, make):
    with pytest.raises(LocalActionError):
        world.acts.open_folder(make(world))
    assert world.opened == []


@pytest.mark.parametrize("bad", [
    "", "   ", "relative/path", "..", "../x", "~", "~/Desktop", "C:foo", "\x00", "a\nb", "x" * 2000,
    "\\\\server\\share\\file.txt", "//server/share/file.txt", "\\\\?\\C:\\Windows", "\\\\.\\C:\\Windows", "\\\\localhost\\c$\\Users",
])
def test_odd_paths_are_refused(world, bad):
    with pytest.raises(LocalActionError):
        world.acts.open_folder(bad)
    with pytest.raises(LocalActionError):
        world.acts.list_folder(bad)


def test_non_string_paths_are_refused(world):
    for bad in (None, 5, ["a"], {"p": 1}):
        with pytest.raises(LocalActionError):
            world.acts.open_file(bad)


@pytest.mark.skipif(not WIN, reason="Windows path rules")
def test_windows_stream_and_trailing_dot_tricks(world):
    (world.safe / "doc.txt").write_text("x", encoding="utf-8")
    for bad in (str(world.safe / "doc.txt") + ":evil.exe", str(world.safe / "doc.txt") + ".", str(world.safe / "doc.txt") + " ",
                str(world.safe / "doc.txt::$DATA"), str(world.safe) + " \\doc.txt"):
        with pytest.raises(LocalActionError):
            world.acts.open_file(bad)
    assert world.opened == []


@pytest.mark.skipif(not WIN, reason="case-insensitive file system")
def test_windows_case_does_not_matter_but_cannot_escape(world):
    assert world.acts.open_folder(str(world.safe).upper())["kind"] == "folder"
    with pytest.raises(LocalActionError):
        world.acts.open_folder(str(world.outside).upper())


def test_other_drive_is_outside(world):
    other = "Z:\\x" if WIN else "/definitely/not/here"
    with pytest.raises(LocalActionError):
        world.acts.open_folder(other)


def test_directory_junction_or_symlink_to_outside_is_refused(world):
    link = world.safe / "shortcut"
    junction(link, world.outside)
    with pytest.raises(LocalActionError, match="outside"):
        world.acts.list_folder(str(link))
    with pytest.raises(LocalActionError, match="outside"):
        world.acts.open_folder(str(link))
    with pytest.raises(LocalActionError, match="outside"):
        world.acts.open_file(str(link / "secret.txt"))
    assert world.opened == []


def test_link_that_stays_inside_is_fine(world):
    link = world.safe / "alias"
    junction(link, world.safe / "Acme")
    assert world.acts.open_folder(str(link))["opened"] == str((world.safe / "Acme").resolve())


def test_file_symlink_to_outside_is_refused(world):
    link = world.safe / "innocent.txt"
    try:
        os.symlink(world.outside / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create file symlinks here")
    with pytest.raises(LocalActionError, match="outside"):
        world.acts.open_file(str(link))


def test_resolution_uses_real_paths_even_when_links_cannot_be_made(world, monkeypatch):
    """Platform independent: if the OS says a path really lives elsewhere, that is what gets judged."""
    real = os.path.realpath
    fake = str(world.safe / "innocent.txt")
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: str(world.outside / "secret.txt") if str(p) == fake else real(p, *a, **k))
    with pytest.raises(LocalActionError, match="outside"):
        world.acts.open_file(fake)
    assert world.opened == []


# ------------------------------------------------------------------------------------------------ roots
def test_roots_never_include_a_drive_root_or_home(tmp_path, monkeypatch):
    anchor = os.path.splitdrive(str(tmp_path))[0] + os.sep
    acts = LocalActions([anchor, str(Path.home()), str(tmp_path)], state_dir=tmp_path / "s")
    roots = acts.roots()
    assert Path(os.path.realpath(tmp_path)) in roots
    assert all(os.path.realpath(anchor) != str(r) and os.path.realpath(Path.home()) != str(r) for r in roots)


def test_env_roots_replace_the_defaults(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("CHIEF_LOCAL_ROOTS", f'{a};"{b}"')
    names = {r.name for r in LocalActions(state_dir=tmp_path / "s").roots()}
    assert names == {"a", "b", "briefs"}


def test_defaults_allow_only_the_demo_folder_and_briefs(tmp_path, monkeypatch):
    """Privacy: Desktop, Documents and Downloads hold real file names that would appear on a projector,
    so they are opt-in through CHIEF_LOCAL_ROOTS and are NOT part of the default allow-list."""
    monkeypatch.delenv("CHIEF_LOCAL_ROOTS", raising=False)
    names = {r.name.lower() for r in LocalActions(state_dir=tmp_path / "s").roots()}
    assert names == {"demo", "briefs"}
    assert local.default_roots() == [local.DEMO_DIR]


def test_briefs_folder_is_allowed_even_with_custom_roots(world):
    brief = world.tmp / "state" / "briefs" / "brief-x.html"
    brief.parent.mkdir(parents=True)
    brief.write_text("<html></html>", encoding="utf-8")
    assert world.acts.open_file(str(brief))["name"] == "brief-x.html"
    (world.tmp / "state" / "memory.json").write_text("[]", encoding="utf-8")
    with pytest.raises(LocalActionError):
        world.acts.open_file(str(world.tmp / "state" / "memory.json"))


def test_config_reads_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEF_LOCAL_ROOTS", f"{tmp_path / 'a'};{tmp_path / 'b'}")
    assert Config.from_env().local_roots == (str(tmp_path / "a"), str(tmp_path / "b"))
    monkeypatch.delenv("CHIEF_LOCAL_ROOTS")
    assert Config.from_env().local_roots == ()


# ------------------------------------------------------------------------------------------ extensions
@pytest.mark.parametrize("ext", sorted(SAFE_EXTENSIONS))
def test_safe_documents_open(world, ext):
    f = world.safe / f"report.{ext}"
    f.write_bytes(b"x")
    assert world.acts.open_file(str(f)) == {"opened": str(f.resolve()), "kind": "file", "name": f.name}
    assert world.opened == [str(f.resolve())]


@pytest.mark.parametrize("name", [
    *[f"tool.{e}" for e in sorted(BLOCKED_EXTENSIONS)],
    "TOOL.EXE", "Setup.Bat", "report.pdf.exe", "invoice.txt.js", "readme", "archive.zip", "photo.svg", "page.htm", "x.pdf.lnk",
])
def test_executables_scripts_and_unknown_types_never_open(world, name):
    f = world.safe / name
    f.write_bytes(b"x")
    with pytest.raises(LocalActionError):
        world.acts.open_file(str(f))
    assert world.opened == []


def test_a_safe_looking_name_pointing_at_an_executable_is_refused(world, monkeypatch):
    real = os.path.realpath
    (world.safe / "ok.pdf").write_bytes(b"x")
    (world.safe / "real.exe").write_bytes(b"x")
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: str(world.safe / "real.exe") if str(p).endswith("ok.pdf") else real(p, *a, **k))
    with pytest.raises(LocalActionError, match="never open"):
        world.acts.open_file(str(world.safe / "ok.pdf"))


def test_open_file_needs_a_real_file_and_open_folder_a_real_folder(world):
    with pytest.raises(LocalActionError):
        world.acts.open_file(str(world.safe / "Acme"))
    with pytest.raises(LocalActionError):
        world.acts.open_file(str(world.safe / "missing.pdf"))
    with pytest.raises(LocalActionError):
        world.acts.open_folder(str(world.safe / "notes.txt"))
    with pytest.raises(LocalActionError):
        world.acts.open_folder(str(world.safe / "nope"))
    assert world.opened == []


def test_nothing_is_ever_modified(world):
    before = sorted(p.relative_to(world.tmp) for p in world.tmp.rglob("*"))
    world.acts.list_folder(str(world.safe))
    world.acts.open_folder(str(world.safe))
    world.acts.open_file(str(world.safe / "notes.txt"))
    assert sorted(p.relative_to(world.tmp) for p in world.tmp.rglob("*")) == before
    assert (world.safe / "notes.txt").read_text(encoding="utf-8") == "hi"


# ------------------------------------------------------------------------------------------ list_folder
def test_list_folder_shape(world):
    (world.safe / ".hidden").write_text("x", encoding="utf-8")
    out = world.acts.list_folder(str(world.safe))
    assert [(e["name"], e["kind"]) for e in out["entries"]] == [("Acme", "folder"), ("notes.txt", "file")]
    assert out["folders"] == 1 and out["files"] == 1 and out["count"] == 2 and not out["truncated"]
    assert {k for e in out["entries"] for k in e} == {"name", "kind", "size", "modified"}  # names only, never contents
    assert "hi" not in repr(out)


def test_list_folder_is_capped(world):
    for i in range(local.MAX_ENTRIES + 7):
        (world.safe / f"f{i:03}.txt").write_text("", encoding="utf-8")
    out = world.acts.list_folder(str(world.safe))
    assert len(out["entries"]) == local.MAX_ENTRIES and out["truncated"] and out["count"] == local.MAX_ENTRIES + 7 + 2


def test_list_folder_does_not_follow_links(world):
    junction(world.safe / "out", world.outside)
    entry = next(e for e in world.acts.list_folder(str(world.safe))["entries"] if e["name"] == "out")
    assert entry["kind"] == "link"


# ------------------------------------------------------------------------------------------ the opener
@pytest.mark.skipif(not WIN, reason="os.startfile is Windows only")
def test_windows_uses_startfile_on_the_resolved_path(world, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", calls.append, raising=False)
    LocalActions([world.safe], state_dir=world.tmp / "s", launcher=REAL_LAUNCHER).open_folder(str(world.safe / ".." / "safe"))
    assert calls == [str((world.safe).resolve())]


@pytest.mark.parametrize("platform,exe", [("darwin", "open"), ("linux", "xdg-open")])
def test_other_systems_use_an_argument_list_never_a_shell(platform, exe, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    monkeypatch.setattr(os, "startfile", lambda *_: pytest.fail("startfile on a non-Windows system"), raising=False)
    REAL_LAUNCHER(str(tmp_path))
    assert seen["cmd"] == [exe, str(tmp_path)] and not seen["kw"].get("shell")


def test_a_missing_opener_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(LocalActionError, match="no way to open"):
        REAL_LAUNCHER(str(tmp_path))


def test_launcher_failure_becomes_a_local_error(world):
    def broken(_):
        raise OSError(5, "Access is denied")

    acts = LocalActions([world.safe], state_dir=world.tmp / "s", launcher=broken)
    with pytest.raises(LocalActionError, match="could not open"):
        acts.open_folder(str(world.safe))


# ------------------------------------------------------------------------------------- mock vs live
@pytest.fixture
def mock(world):
    t = MockTools(NOW)
    t.state_dir = world.tmp / "state"
    t._local = LocalActions([world.safe], state_dir=t.state_dir, launcher=t.opened.append)  # mock keeps its own opener
    return t


@pytest.fixture
def live(world):
    t = LiveTools(Config(mode="live", state_dir=world.tmp / "state", local_roots=(str(world.safe),)), gateway=object())
    t.local.launcher = world.opened.append
    return t


def test_mock_records_the_call_and_opens_nothing(world, monkeypatch):
    monkeypatch.setenv("CHIEF_LOCAL_ROOTS", str(world.safe))
    t = MockTools(NOW)  # the real mock wiring: env roots, its own recording launcher
    t.open_folder(str(world.safe))
    t.open_file(str(world.safe / "notes.txt"))
    assert t.calls == [("open_folder", {"path": str(world.safe.resolve())}), ("open_file", {"path": str((world.safe / "notes.txt").resolve())})]
    assert t.opened == [c[1]["path"] for c in t.calls]  # recorded; os.startfile and Popen were never reached (trap fixture)
    assert t.list_folder(str(world.safe))["count"] == 2
    assert [c[0] for c in t.calls].count("list_folder") == 0  # a read is not a side effect


def test_mock_refuses_exactly_what_live_refuses(mock, live, world):
    bad_paths = [str(world.outside), str(world.safe / ".." / "outside"), "relative", "\\\\srv\\share"]
    for tools in (mock, live):
        for p in bad_paths:
            for name in ("list_folder", "open_folder", "open_file"):
                with pytest.raises(LocalActionError):
                    getattr(tools, name)(p)
        (world.safe / "run.exe").write_bytes(b"x")
        with pytest.raises(LocalActionError):
            tools.open_file(str(world.safe / "run.exe"))
    assert mock.calls == [] and world.opened == []


def test_live_really_launches_through_its_opener(live, world):
    live.open_folder(str(world.safe))
    live.open_file(str(world.safe / "notes.txt"))
    assert world.opened == [str(world.safe.resolve()), str((world.safe / "notes.txt").resolve())]
    assert live.list_folder(str(world.safe))["files"] == 1


def test_live_uses_the_config_roots(world):
    t = LiveTools(Config(mode="live", state_dir=world.tmp / "s", local_roots=(str(world.safe),)), gateway=object())
    assert Path(os.path.realpath(world.safe)) in t.local.roots()


def test_call_dispatch_knows_the_new_tools(mock, world):
    assert mock.call("list_folder", {"path": str(world.safe)})["count"] == 2
    assert mock.call("open_folder", {"path": str(world.safe)})["kind"] == "folder"


# ------------------------------------------------------------------- activity stream and scoping
def test_recording_labels_show_in_the_activity_stream(mock, world):
    bus = ActivityBus()
    rec = RecordingTools(mock, bus)
    rec.list_folder(str(world.safe))
    rec.open_folder(str(world.safe))
    rec.open_file(str(world.safe / "notes.txt"))
    with pytest.raises(LocalActionError):
        rec.open_file(str(world.outside / "secret.txt"))
    ev = bus.since(0)
    titles = [(e.kind, e.title) for e in ev]
    assert ("read", "Looked inside safe: 2 item(s)") in titles
    assert ("write", "Opened the folder safe") in titles
    assert ("write", "Opened the document notes.txt") in titles
    assert any(e.kind == "working" and e.title == "Opening a document on screen..." for e in ev)
    assert any(e.kind == "error" and e.tool == "open_file" for e in ev)


def test_scoped_tools_block_what_a_skill_did_not_declare(mock, world):
    files = load_skills()["files"]
    scoped = ScopedTools(mock, files.allowed_tools, "files")
    assert scoped.list_folder(str(world.safe))["count"] == 2
    assert scoped.open_folder(str(world.safe))["kind"] == "folder"
    for name in ("open_file", "send_email", "create_event", "patch_event", "delete_event", "create_draft", "post_slack", "search_web"):
        with pytest.raises(PermissionError):
            getattr(scoped, name)
    brief = ScopedTools(mock, load_skills()["meeting_prep"].allowed_tools, "meeting_prep")
    for name in ("open_file", "open_folder", "list_folder"):  # meeting_prep never declared them
        with pytest.raises(PermissionError):
            getattr(brief, name)
    only_read = ScopedTools(mock, {"list_folder"}, "x")
    with pytest.raises(PermissionError):
        only_read.open_folder
    assert [c[0] for c in mock.calls] == ["open_folder"]  # only the one the files skill declared ran


def test_skills_declare_no_write_and_only_the_local_tools_they_need():
    skills = load_skills()
    assert skills["files"].allowed_tools == {"list_folder", "open_folder"}
    assert skills["open_brief"].allowed_tools & SAFE_WRITE_TOOLS == {"open_file"}
    for name in ("files", "open_brief"):
        assert not skills[name].allowed_tools & WRITE_TOOLS and skills[name].propose_tools == set()


def test_the_gate_still_rejects_an_open_the_skill_did_not_declare(tmp_path, mock):
    skills = load_skills()
    gate = ApprovalGate(mock, Memory(tmp_path / "m"), skills, lambda p: None)
    for tool in ("open_file", "open_folder", "send_email"):
        with pytest.raises(PermissionError):
            gate.submit(Proposal(skill="files", summary="x", actions=[ToolCall(tool, {"path": "C:/x"})], key=f"k-{tool}"))
    assert not gate.pending

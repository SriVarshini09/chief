"""Safe local actions: look inside a few allow-listed folders and put a folder or document on screen.

Three tools, implemented once here and exposed by both MockTools (records the call, opens nothing) and
LiveTools (really opens):

    list_folder(path)   READ        names, kinds, sizes: never file contents
    open_folder(path)   SAFE_WRITE  shows the folder in the file manager (UI only)
    open_file(path)     SAFE_WRITE  opens a document in its default viewer (UI only)

They only ever OPEN. Nothing here deletes, moves, writes, renames or runs anything.

Safety rules (all covered by tests/test_local_tools.py):
  * A path must be absolute and, after resolving symlinks, junctions and `..`, sit inside an allow-listed root.
    UNC paths (\\\\server\\share), device paths (\\\\?\\, \\\\.\\), NTFS alternate data streams and names that end in
    a dot or space (which Windows silently strips) are refused outright.
  * Roots: CHIEF_LOCAL_ROOTS (";"-separated) or, by default, Desktop, Documents, Downloads and the project's
    demo folder. `<state_dir>/briefs` (where Chief saves its own one-page briefs) is always allowed too.
    A drive root or your whole home folder is never accepted as a root.
  * open_file only opens an extension from SAFE_EXTENSIONS. Executables, scripts, shortcuts, installers and
    macro-enabled Office files are refused, whatever the allow-list says.
  * The opener is os.startfile on Windows; `open` / `xdg-open` elsewhere, called with an argument list, never
    through a shell. The only argument is the already-resolved absolute path, not text taken from the request.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from chief.config import ROOT

MAX_ENTRIES = 200
MAX_PATH = 1024

SAFE_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "gif", "txt", "md", "csv", "html", "docx", "xlsx", "pptx"})
# Never opened, even if someone adds them to SAFE_EXTENSIONS by mistake. SAFE_EXTENSIONS is the real gate;
# this list exists so the refusal can say why, and so a test pins the dangerous ones by name.
BLOCKED_EXTENSIONS = frozenset(
    "exe com scr pif msi msp msix appx bat cmd ps1 psm1 psd1 ps1xml psc1 vbs vbe js jse wsf wsh hta cpl reg lnk url scf "
    "dll sys drv ocx jar py pyw pyc pyz sh bash zsh command app dmg pkg deb rpm apk appref-ms application gadget inf "
    "chm iso img vhd vhdx docm xlsm pptm dotm xltm potm ppam xlam sldm library-ms search-ms".split()
)


class LocalActionError(Exception):
    """A request was refused or could not be done. The message is safe to show the user."""


def parse_roots(raw: str | None) -> list[str]:
    return [p.strip().strip('"') for p in re.split(r"[;\r\n]+", raw or "") if p.strip().strip('"')]


# --------------------------------------------------------------------------------------- default folders
def _known_folder(guid: str) -> Path | None:
    """Windows 'known folder' (Desktop/Documents/Downloads follow OneDrive redirection). None elsewhere."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        parts = guid.strip("{}").split("-")
        g = _GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(parts[3] + parts[4])))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 - any failure just means "use the home-folder guess"
        return None


_KNOWN = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}
DEMO_DIR = ROOT / "demo"


def state_briefs_dir(state_dir: Path | str | None = None) -> Path:
    return Path(state_dir or os.getenv("CHIEF_STATE_DIR") or ROOT / ".chief") / "briefs"


def default_folders(home: Path | None = None) -> dict[str, list[Path]]:
    """name -> candidate paths, the preferred one first. Used both for the default allow-list and for the
    files skill's spoken names, so the two can never disagree."""
    home = Path(home) if home else Path.home()
    out: dict[str, list[Path]] = {}
    for name in ("desktop", "documents", "downloads"):
        title = name.title()
        cands = [_known_folder(_KNOWN[name]) if home == Path.home() else None, home / title]
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.getenv(var):
                cands.append(Path(os.environ[var]) / title)
        out[name] = list(dict.fromkeys(c for c in cands if c))
    out["demo"] = [DEMO_DIR]
    return out


def default_roots(home: Path | None = None) -> list[Path]:
    """Only the project's demo folder is allowed by default. Desktop, Documents and Downloads hold real names
    and files that would show up on a projector, so they are opt-in: list them in CHIEF_LOCAL_ROOTS."""
    return list(default_folders(home)["demo"])


# ------------------------------------------------------------------------------------------ the actions
def _norm(p: str) -> str:
    return os.path.normcase(p)


def _inside(real: str, root: str) -> bool:
    try:
        return os.path.commonpath([_norm(real), _norm(root)]) == _norm(root)
    except ValueError:  # different drives, or one is not absolute
        return False


def _default_launcher(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606 - target is a resolved, allow-listed path
        return
    cmd = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
    try:  # argument list, no shell: `target` is an absolute path, so it cannot be read as an option
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        raise LocalActionError(f"this machine has no way to open things ({cmd[0]} failed: {exc.strerror or exc})") from exc


class LocalActions:
    def __init__(
        self,
        roots: Iterable[str | Path] | None = None,
        state_dir: Path | str | None = None,
        launcher: Callable[[str], None] | None = None,
        home: Path | None = None,
    ):
        """roots=None means: CHIEF_LOCAL_ROOTS if set (read on every call), else the defaults."""
        self._roots = None if roots is None else [str(r) for r in roots]
        self._state_dir, self._home = state_dir, home
        self.launcher = launcher or _default_launcher

    # --- allow-list
    def configured_roots(self) -> list[Path]:
        raw = self._roots if self._roots is not None else parse_roots(os.getenv("CHIEF_LOCAL_ROOTS"))
        roots = [Path(r) for r in raw] if raw else default_roots(self._home)
        return roots + [state_briefs_dir(self._state_dir)]

    def roots(self) -> list[Path]:
        """Resolved roots that are safe to accept (never a drive root or the whole home folder)."""
        home = _norm(os.path.realpath(self._home or Path.home()))
        out: list[Path] = []
        for r in self.configured_roots():
            if not str(r).strip() or not os.path.isabs(str(r)):
                continue
            real = os.path.realpath(str(r))
            if _norm(real) == _norm(os.path.splitdrive(real)[0] + os.sep) or _norm(real) == home or real == os.path.dirname(real):
                continue
            if Path(real) not in out:
                out.append(Path(real))
        return out

    def resolve(self, path: str | os.PathLike) -> Path:
        """Absolute, symlink-free path inside the allow-list, or LocalActionError."""
        text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
        if not text.strip() or len(text) > MAX_PATH or any(ord(c) < 32 for c in text):
            raise LocalActionError("that is not a usable path")
        if text.startswith(("\\\\", "//")):
            raise LocalActionError("network (UNC) and device paths are not allowed")
        if not os.path.isabs(text) or text[:2] in ("~\\", "~/"):
            raise LocalActionError("I need a full path (one that starts at a drive or '/'), not a relative one")
        if os.name == "nt":
            if ":" in text[2:]:
                raise LocalActionError("alternate data streams are not allowed")
            for part in re.split(r"[\\/]+", text):
                if part not in ("", ".", "..") and part != part.rstrip(" ."):
                    raise LocalActionError("names ending in a dot or a space are not allowed")
        real = os.path.realpath(text)
        roots = self.roots()
        if not any(_inside(real, str(r)) for r in roots):
            raise LocalActionError(f"that location is outside the folders I may open ({self.describe_roots(roots)})")
        return Path(real)

    @staticmethod
    def describe_roots(roots: list[Path]) -> str:
        return ", ".join(dict.fromkeys(r.name or str(r) for r in roots)) or "none configured"

    # --- tools
    def list_folder(self, path: str) -> dict:
        real = self.resolve(path)
        if not real.is_dir():
            raise LocalActionError(f"{real.name or real} is not a folder I can list")
        entries, total = [], 0
        try:
            with os.scandir(real) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    total += 1
                    if len(entries) >= MAX_ENTRIES:
                        continue
                    link = e.is_symlink() or getattr(e, "is_junction", lambda: False)()
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        st = None
                    kind = "link" if link else "folder" if e.is_dir(follow_symlinks=False) else "file"
                    entries.append(
                        {
                            "name": e.name,
                            "kind": kind,
                            "size": st.st_size if st and kind == "file" else None,
                            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds") if st else None,
                        }
                    )
        except OSError as exc:
            raise LocalActionError(f"could not read {real.name or real}: {exc.strerror or exc}") from exc
        entries.sort(key=lambda x: (x["kind"] != "folder", x["name"].casefold()))
        return {
            "path": str(real),
            "entries": entries,
            "count": total,
            "folders": sum(1 for x in entries if x["kind"] == "folder"),
            "files": sum(1 for x in entries if x["kind"] == "file"),
            "truncated": total > len(entries),
        }

    def open_folder(self, path: str) -> dict:
        real = self.resolve(path)
        if not real.is_dir():
            raise LocalActionError(f"{real.name or real} is not a folder that exists")
        self._launch(str(real))
        return {"opened": str(real), "kind": "folder"}

    def open_file(self, path: str) -> dict:
        real = self.resolve(path)
        for name in (Path(os.fspath(path)).name, real.name):  # the name asked for, and where it really points
            ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
            if ext in BLOCKED_EXTENSIONS:
                raise LocalActionError(f"I never open .{ext} files (programs, scripts and shortcuts are off limits)")
            if ext not in SAFE_EXTENSIONS:
                raise LocalActionError(f"I only open these file types: {', '.join(sorted(SAFE_EXTENSIONS))}")
        if not real.is_file():
            raise LocalActionError(f"{real.name} is not a file that exists")
        self._launch(str(real))
        return {"opened": str(real), "kind": "file", "name": real.name}

    def _launch(self, target: str) -> None:
        try:
            self.launcher(target)
        except LocalActionError:
            raise
        except OSError as exc:
            raise LocalActionError(f"could not open it: {exc.strerror or exc}") from exc


# ------------------------------------------------------------------------------- spoken names for folders
def named_folders(home: Path | None = None, state_dir: Path | str | None = None) -> dict[str, list[Path]]:
    """Spoken name -> the folder(s) it can mean, built from the same allow-list the tools enforce.

    Default roots give desktop / documents / downloads / demo (the preferred existing location of each) plus
    briefs. With CHIEF_LOCAL_ROOTS set, each root answers to its own folder name. More than one path under a
    name means the name is ambiguous: callers must ask, never guess.
    """
    out: dict[str, list[Path]] = {}
    if parse_roots(os.getenv("CHIEF_LOCAL_ROOTS")):
        briefs = Path(os.path.realpath(state_briefs_dir(state_dir)))
        for root in LocalActions(None, state_dir, home=home).roots():
            if root != briefs:
                out.setdefault(root.name.casefold(), []).append(root)
    else:
        for name, cands in default_folders(home).items():
            out[name] = [next((c for c in cands if c.is_dir()), cands[0])]
    out.setdefault("briefs", [state_briefs_dir(state_dir)])
    return out

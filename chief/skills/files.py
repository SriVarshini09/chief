"""files: "open my desktop", "open the demo folder", "what's in my downloads".

Opens an allow-listed folder on screen and says, in a sentence, what is in it. Strictly read-only: it proposes
nothing, never writes, moves or deletes, and never opens a file (that is open_brief's job, for one saved page).

Name resolution is deterministic, no model involved: the spoken name must equal one of the allow-listed
folder names (chief.tools.local.named_folders), optionally followed by "in/on my <folder>" to pick one
sub-folder by its exact name. Unknown, empty or ambiguous names are refused with a list of what can be opened.
The tools re-check every path against the allow-list, so this module is a convenience, not the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

from chief.models import Proposal
from chief.skills.base import Context, Skill
from chief.tools.local import named_folders

SPOKEN = {  # extra spoken forms for the built-in names; custom roots answer to their own folder name only
    "desktop": ["desktop"],
    "documents": ["documents"],
    "downloads": ["downloads", "download"],
    "demo": ["demo", "demo folder"],
    "briefs": ["briefs"],
}
LIST_WORDS = ("what's in", "whats in", "what is in", "what's inside", "what is inside", "list", "contents of")
OPEN_WORDS = ("open", "show me", "show", "pull up", "bring up", "go to", "look in", "look inside")
FILLER = {"my", "the", "a", "an", "me", "up", "please", "folder", "directory", "dir", "files", "file"}
PREPOSITIONS = (" in ", " on ", " inside ", " under ", " within ")
SHOWN = 5


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("’", "'")).split())


def _clean(name: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:60]


def _triggers() -> list[str]:
    nouns = ["desktop", "documents", "downloads", "demo folder", "downloads folder", "documents folder", "briefs folder", "briefs"]
    verbs = ["open my", "open the", "show me my", "show me the", "pull up my", "pull up the"]
    ask = ["what's in my", "what's in the", "whats in my", "what is in my", "what is in the"]
    out = [f"{v} {n}" for v in verbs + ask for n in nouns]
    return out + ["open a folder", "open my folder", "open the folder", "open folder"]


def _phrase(said: str) -> tuple[bool, str]:
    """(only_list, the rest) with the leading verb removed. A question like "what's in ..." only lists."""
    for w in sorted(LIST_WORDS + OPEN_WORDS, key=len, reverse=True):
        if said == w or said.startswith(w + " "):
            return w in LIST_WORDS, said[len(w):].strip()
    return False, said


def _split_sub(phrase: str) -> tuple[str, str]:
    """'acme in my documents' -> ('acme', 'my documents'); no preposition -> ('', phrase)."""
    padded = f" {phrase} "
    hits = [(padded.find(p), p) for p in PREPOSITIONS if p in padded]
    if not hits:
        return "", phrase
    pos, p = min(hits)
    return padded[:pos].strip(), padded[pos + len(p):].strip()


def _words(phrase: str) -> str:
    return " ".join(w for w in phrase.split() if w not in FILLER)


def resolve_root(phrase: str, folders: dict[str, list[Path]]) -> tuple[str | None, str]:
    """-> (name, "") on success, or (None, reason) for an empty, unknown or ambiguous name."""
    wanted = _words(phrase)
    if not wanted:
        return None, "Which folder?"
    hits = [n for n in folders if wanted == n or wanted in SPOKEN.get(n, [])]
    if not hits:
        return None, f'I don\'t know a folder called "{wanted}".'
    if len(hits) > 1 or len(folders[hits[0]]) > 1:
        options = [str(p) for n in hits for p in folders[n]]
        return None, f'"{wanted}" could mean more than one folder ({"; ".join(options)}).'
    return hits[0], ""


def _refuse(why: str, folders: dict[str, list[Path]], key: str) -> Proposal:
    return Proposal(skill="files", summary=f"{why} I can open: {', '.join(sorted(folders))}.", actions=[], key=f"files:refused:{key}")


def _sentence(listing: dict, label: str, opened: bool) -> str:
    n = listing["count"]
    if n == 0:
        return f"Opened {label} on your screen. It is empty." if opened else f"{label} is empty."
    names = [_clean(e["name"]) + ("/" if e["kind"] == "folder" else "") for e in listing["entries"][:SHOWN]]
    more = f", and {n - len(names)} more" if n > len(names) else ""
    body = f"{n} item(s) ({listing['folders']} folder(s), {listing['files']} file(s)): {', '.join(names)}{more}."
    return f"Opened {label} on your screen. {body}" if opened else f"{label} holds {body}"


def run(ctx: Context) -> Proposal | None:
    said = _norm(ctx.request)
    if not said:
        return None
    folders = named_folders(state_dir=ctx.memory.path.parent)
    only_list, phrase = _phrase(said)
    sub, root_phrase = _split_sub(phrase)
    if sub and not _words(sub):
        sub = ""  # "the folder in my documents" names no sub-folder
    name, why = resolve_root(root_phrase, folders)
    if name is None:
        return _refuse(why, folders, _words(phrase) or "none")
    target = folders[name][0]
    label = name.title() if name != "demo" else "the demo folder"

    simulated = False
    try:
        if sub:
            wanted = _norm(_words(sub))
            subdirs = [e["name"] for e in ctx.tools.list_folder(str(target))["entries"] if e["kind"] == "folder"]
            kids = [d for d in subdirs if _norm(d) == wanted]
            if len(kids) != 1:
                why = f'"{wanted}" matches {len(kids)} folders in {label}.' if kids else f'There is no folder called "{wanted}" in {label}.'
                return Proposal(
                    skill="files", actions=[], key=f"files:refused:{name}:{wanted}",
                    summary=f"{why} Folders there: {', '.join(_clean(d) for d in subdirs[:8]) or 'none'}.",
                )
            target, label = target / kids[0], f"{_clean(kids[0])} in {label}"
        if not only_list:
            res = ctx.tools.open_folder(str(target))
            simulated = isinstance(res, dict) and bool(res.get("simulated"))
        listing = ctx.tools.list_folder(str(target))
    except Exception as exc:  # LocalActionError, or the demo wrapper's RuntimeError around it: say it, never crash
        verb = "list" if only_list else "open"
        return Proposal(skill="files", summary=f"I couldn't {verb} {label}: {exc}.", actions=[], key=f"files:error:{name}")

    text = _sentence(listing, label, opened=not only_list)
    if simulated:  # mock mode: be honest that nothing actually opened on the screen
        text = text.replace(f"Opened {label} on your screen.", f"(Mock mode: nothing was really opened.) {label}:", 1)
    if only_list:
        text += f' Say "open {_words(root_phrase)}" and I\'ll put it on screen.'
    return Proposal(skill="files", summary=text, actions=[], key=f"files:{name}:{'list' if only_list else 'open'}")


SKILL = Skill(
    name="files",
    description="Open your desktop, documents, downloads or demo folder on screen and say what is in it (read-only).",
    triggers=_triggers(),
    allowed_tools={"list_folder", "open_folder"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

"""open_brief: "show me the brief". Puts the one-page brief for your next outside meeting on screen.

If meeting_prep already saved the brief (under <state_dir>/briefs) it is reused; otherwise it is built now from
the same reads meeting_prep uses. The only side effect is open_file on that one saved page: the tool itself
refuses anything outside the allow-list or any file type that is not a document (chief/tools/local.py).
Nothing is written to your calendar or mail, and the result is read-only.
"""

from __future__ import annotations

from pathlib import Path

from chief.briefs import find_brief
from chief.models import Proposal
from chief.skills import meeting_prep
from chief.skills.base import Context, Skill


def _answer(text: str, key: str, sources: list[str] | None = None) -> Proposal:
    return Proposal(skill="open_brief", summary=text, actions=[], key=key, sources=sources or [])


def run(ctx: Context) -> Proposal | None:
    meeting = meeting_prep.next_external_meeting(ctx)
    if meeting is None:
        return _answer("I don't see a meeting with anyone outside your team in the next day and a half, so there is no brief to show.", "open_brief:none")
    state_dir = ctx.memory.path.parent
    path: Path | None = find_brief(state_dir, meeting.id)
    sources: list[str] = []
    if path is None:
        prep = meeting_prep.gather(ctx, meeting)
        if prep is None:
            return _answer(f"I found nothing yet to put in a brief for {meeting.title}: no earlier email and no web background.", f"open_brief:{meeting.id}")
        path = meeting_prep.save_prep_brief(ctx, prep)
        sources = [r["url"] for r in prep.web]
        if path is None:
            return _answer("I could not save the brief to disk, so there is nothing to open.", f"open_brief:{meeting.id}")
    simulated = False
    try:
        res = ctx.tools.open_file(str(path))
        simulated = isinstance(res, dict) and bool(res.get("simulated"))
    except Exception as exc:  # LocalActionError, or the demo wrapper's RuntimeError around it: say it, never crash
        return _answer(f"The brief for {meeting.title} is saved at {path}, but I could not open it: {exc}.", f"open_brief:{meeting.id}", sources)
    opened = "(Mock mode: nothing was really opened.) The" if simulated else "Opened the"
    where = "" if simulated else " on your screen"
    return _answer(f"{opened} one-page brief for {meeting.title}{where}. It is saved at {path} if you want to print it.", f"open_brief:{meeting.id}", sources)


SKILL = Skill(
    name="open_brief",
    description="Put the one-page brief for your next outside meeting on screen (builds it first if needed).",
    triggers=["show me the brief", "open the brief", "open my brief", "pull up the brief", "show the brief", "show me my brief", "open the meeting brief"],
    allowed_tools={"list_events", "fetch_emails", "fetch_thread", "search_web", "open_file"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

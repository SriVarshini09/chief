"""guide: "guide me there", "start walking", "stop guiding".

Read-only. Starts (or stops) spoken turn-by-turn walking guidance to the place you just asked about, from where
you are. The page then follows your live location and speaks each turn; on Telegram, share a live location and
Chief sends the next instruction near each turn. The guide lives in memory only (chief/guide.py).
"""

from __future__ import annotations

import re
import uuid

from chief import guide
from chief.models import Proposal
from chief.places import CREDIT
from chief.skills.base import Context, Skill
from chief.skills.nearby import ask_for_location, pick

_STOP = re.compile(r"\b(stop|end|cancel|quit|finish)\b.*\b(guid\w*|walk\w*|navigat\w*|directions)\b|\bi'?m there\b", re.I)
_TO = re.compile(r"\b(?:guide|walk)\s+me\s+(?:to|towards)\s+(?P<to>.+?)[\s?.!]*$", re.I)


def _id() -> str:
    return f"guide:{uuid.uuid4().hex}"


def run(ctx: Context) -> Proposal:
    text = ctx.request.strip()
    if _STOP.search(text):
        out = guide.stop(ctx.where)
        return Proposal("guide", out["say"] or "I wasn't guiding you anywhere.", [], _id(), needs="guide_stop")
    here = ctx.where.here(ctx.now)
    m = _TO.search(text)
    said = m.group("to").strip() if m else ""
    dest = None
    if said and said.lower() not in ("there", "it", "that place"):
        dest = pick(ctx, said) or ctx.tools.find_location(said, here.lat if here else None, here.lon if here else None)
        if dest is None:
            return Proposal("guide", f"I couldn't find {said} on the map. Try the full name or a street.", [], _id())
        ctx.where.chosen = dest
    if ctx.where.chosen is None:
        return Proposal("guide", "Where to? Ask for directions first, like: directions to the first one. Then say: guide me there.", [], _id())
    if here is None:
        return ask_for_location(ctx, "guide", "To talk you there turn by turn, I need your live location.")
    to = {"lat": ctx.where.chosen.lat, "lon": ctx.where.chosen.lon, "name": ctx.where.chosen.name}
    out = guide.start(ctx.where, ctx.tools, here.lat, here.lon, to)
    if not out["active"]:
        return Proposal("guide", out["say"], [], _id(), sources=[CREDIT])
    say = out["say"] + " Watch the road, not the screen."
    return Proposal("guide", say, [], _id(), sources=[CREDIT], needs="guide")


SKILL = Skill(
    label="Walking guide",
    name="guide",
    description="Talks you to a place on foot, turn by turn, from your live location: guide me there, start walking, stop guiding.",
    triggers=[
        "guide me", "start walking", "start guiding", "walk me there", "walk me to", "turn by turn",
        "stop guiding", "stop the guide", "stop walking", "stop navigation", "stop navigating", "end guidance",
    ],
    allowed_tools={"find_location", "get_route"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

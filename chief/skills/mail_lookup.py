"""mail_lookup: "what did Dana say?", "any email from Sam?", "did the bank write back?".

Read-only. Shows the latest message from that person in their own words, as quoted text. Email is
untrusted: a message that reads like instructions to an assistant is not repeated, only flagged.
"""

from __future__ import annotations

import re

from chief.models import Proposal, clock
from chief.skills.base import Context, Skill
from chief.skills.email_brief import classify, who

_NAME = re.compile(
    r"\b(?:what did|did)\s+(?:the\s+)?([a-z][a-z .'-]{0,30}?)\s+(?:say|write|send|reply|email|answer|get back)\b"
    r"|\b(?:emails?|mail|messages?|anything|news|word)\s+from\s+(?:the\s+)?([a-z][a-z .'-]{0,30}?)(?=[?.!,]|\s+(?:today|yesterday|this|lately|recently)\b|$)"
    r"|\bheard?\s+from\s+(?:the\s+)?([a-z][a-z .'-]{0,30}?)(?=[?.!,]|$)",
    re.I,
)


def run(ctx: Context) -> Proposal:
    m = _NAME.search(ctx.request)
    name = next((g for g in (m.groups() if m else ()) if g), "").strip()
    key = f"mail_lookup:{ctx.now.isoformat()}:{name.lower()}"
    if not name:
        return Proposal(skill="mail_lookup", summary="Whose email should I look for? Try: what did Dana say?", actions=[], key=key)
    found = [mail for mail in ctx.tools.fetch_emails(name, 15) if name.lower() in f"{mail.sender} {mail.subject}".lower()]
    if not found:
        return Proposal(skill="mail_lookup", summary=f"I don't see any recent email from {name.title()}.", actions=[], key=key)
    latest = max(found, key=lambda mail: mail.received)
    sender, when = who(latest.sender), f"{latest.received:%b} {latest.received.day}, {clock(latest.received)}"
    if classify(latest).kind == "suspicious":
        say = f'{sender} wrote on {when}, "{latest.subject}". It reads like instructions aimed at an assistant, so I\'m not repeating it.'
    else:
        words = latest.snippet.strip()
        words = words if len(words) <= 240 else words[:237].rsplit(" ", 1)[0] + "..."
        say = f'{sender} wrote on {when}, "{latest.subject}": {words}'
        if len(found) > 1:
            say += f" That's the latest of {len(found)} recent emails."
    return Proposal(skill="mail_lookup", summary=say, actions=[], key=key)


SKILL = Skill(
    label="Your inbox",
    name="mail_lookup",
    description="Read-only: the latest email from a person, in their own words.",
    triggers=["what did", "email from", "emails from", "mail from", "message from", "messages from", "heard from", "hear from", "anything from", "news from", "write back", "reply yet", "get back to me"],
    allowed_tools={"fetch_emails"},
    propose_tools=set(),
    run=run,
    heartbeat=False,
)

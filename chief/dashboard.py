"""What the garden dashboard shows: today, the week, mail that needs you, and rest.

Reads through the unrecorded tools, so refreshing the page never fills the activity stream. Each part
fails on its own: if mail is down the calendar still shows, and the page says which part is missing.
Mail subjects and senders are shown as text; message bodies are never sent to the page.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from chief.models import clock
from chief.runtime import Runtime
from chief.skills.audit import HEAVY_MEETINGS, SHORT_SLEEP_H
from chief.skills.email_brief import MAX_MAILS, WINDOW_HOURS, Item, classify, who


def _midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _timed(events: list) -> list:
    return sorted((e for e in events if 0 < e.minutes < 24 * 60), key=lambda e: e.start)


def today(rt: Runtime, now: datetime) -> list[dict]:
    start = _midnight(now)
    return [
        {
            "title": e.title,
            "start": e.start.hour + e.start.minute / 60,
            "end": e.end.hour + e.end.minute / 60,
            "minutes": e.minutes,
            "clock": clock(e.start),
        }
        for e in _timed(rt.inner_tools.list_events(start, start + timedelta(days=1)))
    ]


def week(rt: Runtime, now: datetime) -> list[dict]:
    start = _midnight(now)
    events = _timed(rt.inner_tools.list_events(start, start + timedelta(days=7)))
    days = []
    for offset in range(7):
        day = (start + timedelta(days=offset)).date()
        count = sum(1 for e in events if e.start.date() == day)
        days.append({"day": f"{day:%a}", "full": f"{day:%A}", "meetings": count, "heavy": count >= HEAVY_MEETINGS})
    return days


def _mail_item(item: Item) -> dict:
    return {"from": who(item.mail.sender), "subject": item.mail.subject, "deadline": item.deadline}


def inbox(rt: Runtime, now: datetime) -> dict:
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    mails = [m for m in rt.inner_tools.fetch_emails("", MAX_MAILS) if m.unread and m.received >= cutoff]
    items = [classify(m) for m in mails]
    of_kind = lambda kind: [i for i in items if i.kind == kind]  # noqa: E731
    return {
        "reply": [_mail_item(i) for i in of_kind("reply")],
        "deadline": [_mail_item(i) for i in of_kind("deadline")],
        "fyi": len(of_kind("fyi")),
        "flagged": len(of_kind("suspicious")),
    }


def rest(rt: Runtime) -> dict:
    nights = rt.inner_tools.read_health(14)[-14:]
    hours = [round(n.hours, 1) for n in nights]
    return {
        "nights": [{"day": f"{n.day:%a}", "hours": round(n.hours, 1)} for n in nights],
        "average": round(sum(hours) / len(hours), 1) if hours else None,
        "short_under": SHORT_SLEEP_H,
        "sample": True,  # Chief has no real health import yet: see the README
    }


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _names(items: list[dict]) -> str:
    names = list(dict.fromkeys(i["from"] for i in items))
    shown = names[:2] + ([f"{len(names) - 2} more"] if len(names) > 2 else [])
    return " and ".join(shown) if len(shown) < 3 else f"{shown[0]}, {shown[1]} and {shown[2]}"


def opening(hour: float, meetings: list[dict], mail: dict, days: list[dict], missing: list[str]) -> str:
    """The first thing Chief says: built in plain code from what it read, with no model involved."""
    hello = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    lines = [f"Hi, {hello.lower()}! I'm glad you're here."]
    if "calendar" in missing:
        lines.append("I'm sorry, I can't reach your calendar right now.")
    elif meetings:
        first = meetings[0]
        ahead = [m for m in meetings if m["end"] > hour]
        if not ahead:  # evening: don't greet with a meeting that already happened
            lines.append(f"Your {_plural(len(meetings), 'meeting')} for today {'is' if len(meetings) == 1 else 'are'} done. Well done.")
        elif ahead[0] is not first:
            lines.append(f"You've got {_plural(len(ahead), 'meeting')} left today. Next is {ahead[0]['title']} at {ahead[0]['clock']}.")
        else:
            lines.append(f"You've got {_plural(len(meetings), 'meeting')} today, starting with {first['title']} at {first['clock']}.")
    else:
        lines.append("Your calendar's clear today.")
    if "mail" in missing:
        lines.append("I'm sorry, I can't reach your email right now.")
    elif mail["reply"]:
        lines.append(f"{_names(mail['reply'])} {'is' if len(mail['reply']) == 1 else 'are'} waiting to hear from you.")
    else:
        lines.append("And nothing in your inbox needs you.")
    if mail["flagged"]:
        lines.append(f"I ignored {_plural(mail['flagged'], 'email')} that tried to give me orders.")
    heavy = next((d for d in days[1:] if d["heavy"]), None)
    if heavy and len(lines) < 4:
        lines.append(f"{heavy['full']} looks heavy, so I'm keeping an eye on it.")
    if len(lines) < 4:
        lines.append("I'll be right here whenever you need me.")
    return " ".join(lines)


def _part(name: str, read: Callable[[], Any], fallback: Any, missing: list[str]) -> Any:
    try:
        return read()
    except Exception:  # one service being down must not blank the whole page
        missing.append(name)
        return fallback


def snapshot(rt: Runtime) -> dict:
    now = rt.now()
    missing: list[str] = []
    meetings = _part("calendar", lambda: today(rt, now), [], missing)
    days = _part("week", lambda: week(rt, now), [], missing)
    mail = _part("mail", lambda: inbox(rt, now), {"reply": [], "deadline": [], "fyi": 0, "flagged": 0}, missing)
    hour = now.hour + now.minute / 60
    return {
        "hour": hour,
        "date": f"{now:%A}, {now:%B} {now.day}",
        "mode": rt.cfg.mode,
        "say": opening(hour, meetings, mail, days, missing),
        "today": meetings,
        "week": days,
        "inbox": mail,
        "rest": _part("sleep", lambda: rest(rt), None, missing),
        "missing": missing,
    }

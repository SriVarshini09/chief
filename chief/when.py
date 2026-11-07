"""Reading dates, times and lengths out of what people say: "tomorrow", "on friday", "6pm", "half an hour".

Plain code, no model. Anything it cannot read comes back as None, and the skill asks instead of guessing.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_WORD = re.compile(r"\b(today|tonight|tomorrow|day after tomorrow|" + "|".join(WEEKDAYS) + r")\b", re.I)
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])|\b(\d{1,2}):(\d{2})\b|\b(noon|midday|midnight)\b", re.I)
_AT_BARE = re.compile(r"\bat\s+(\d{1,2})(?![\d:])(?!\s*(?:min|hour|h\b))", re.I)
_LENGTH = re.compile(r"\b(half an hour|an hour|(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?|h)\b)", re.I)


def day_in(text: str, now: datetime) -> date | None:
    """The day a request is about, or None when it does not say."""
    m = _DAY_WORD.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    today = now.date()
    if word in ("today", "tonight"):
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    if word == "day after tomorrow":
        return today + timedelta(days=2)
    return today + timedelta(days=(WEEKDAYS.index(word) - today.weekday()) % 7)


def times_in(text: str) -> list[tuple[int, int]]:
    """Every clock time in the text, in order, as (hour, minute) on a 24-hour clock."""
    found = []
    for m in _CLOCK.finditer(text):
        if m.group(6):
            found.append((m.start(), (0 if m.group(6).lower() == "midnight" else 12, 0)))
            continue
        if m.group(4):
            hour, minute = int(m.group(4)), int(m.group(5))
            if hour < 24 and minute < 60:
                found.append((m.start(), (hour, minute)))
            continue
        hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()[0]
        if not 1 <= hour <= 12 or minute > 59:
            continue
        hour = hour % 12 + (12 if half == "p" else 0)
        found.append((m.start(), (hour, minute)))
    for m in _AT_BARE.finditer(text):                 # "at 6": people mean the afternoon unless it is plainly the morning
        hour = int(m.group(1))
        if 1 <= hour <= 12 and not any(abs(at - m.start()) < 6 for at, _ in found):
            found.append((m.start(), (hour + 12 if hour <= 7 else hour, 0)))
    return [t for _, t in sorted(found)]


def length_in(text: str) -> int | None:
    """A length in minutes ("30 minutes", "an hour", "1.5 hours"), or None."""
    m = _LENGTH.search(text)
    if not m:
        return None
    if m.group(1).lower() == "half an hour":
        return 30
    if m.group(1).lower() == "an hour":
        return 60
    amount, unit = float(m.group(2)), m.group(3).lower()
    return round(amount * (60 if unit.startswith("h") else 1))


def at(day: date, hm: tuple[int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, hm[0], hm[1])


def say_day(day: date, now: datetime) -> str:
    today = now.date()
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if 0 < (day - today).days < 7:
        return f"on {day:%A}"
    return f"on {day:%A %B} {day.day}"

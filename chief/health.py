"""Sleep data. The demo uses a seeded sample; real exports vary by app version and device."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from chief.models import SleepDay

# Mon/Tue/Thu are the heavy meeting days in the seeded calendar (see chief/tools/mock.py).
HEAVY_WEEKDAYS = {0, 1, 3}
_AFTER_HEAVY = [5.1, 5.4, 6.3, 5.6, 5.8, 5.2]  # mostly short nights after heavy days
_OTHER = [7.2, 7.5, 7.0, 7.8]


def generate_sample(now: datetime, days: int = 14) -> list[SleepDay]:
    """Deterministic sample: short sleep the night after a heavy meeting day."""
    out: list[SleepDay] = []
    heavy_i = other_i = 0
    for offset in range(days, -1, -1):
        morning = now.date() - timedelta(days=offset)
        prev = morning - timedelta(days=1)
        if prev.weekday() in HEAVY_WEEKDAYS:
            hours = _AFTER_HEAVY[heavy_i % len(_AFTER_HEAVY)]
            heavy_i += 1
        else:
            hours = _OTHER[other_i % len(_OTHER)]
            other_i += 1
        out.append(SleepDay(day=morning, hours=hours, steps=4200 if hours < 6 else 8100))
    return out


def write_sample_csv(path: Path, now: datetime, days: int = 14) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "sleep_hours", "steps"])
        for s in generate_sample(now, days):
            w.writerow([s.day.isoformat(), s.hours, s.steps])


def read_csv(path: Path) -> list[SleepDay]:
    """Reads the simple format above (date, sleep_hours, steps)."""
    out: list[SleepDay] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            steps = row.get("steps")
            out.append(
                SleepDay(
                    day=date.fromisoformat(row["date"]),
                    hours=float(row["sleep_hours"]),
                    steps=int(steps) if steps else None,
                )
            )
    return out

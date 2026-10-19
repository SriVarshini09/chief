"""Memory and activity log. Plain files you can read, edit, or delete."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


class Memory:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # parallel tasks write memory at the same time
        self.path = state_dir / "memory.json"
        self.log_path = state_dir / "activity.jsonl"
        self.records: list[dict] = json.loads(self.path.read_text("utf-8")) if self.path.exists() else []

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.records, indent=2), encoding="utf-8")

    def record(self, *, key: str, skill: str, decision: str, summary: str) -> None:
        with self._lock:
            self.records.append(
                {"key": key, "skill": skill, "decision": decision, "summary": summary, "at": datetime.now().isoformat(timespec="seconds")}
            )
            self._save()

    def decided(self, key: str) -> bool:
        with self._lock:
            return any(r["key"] == key for r in self.records)

    def decisions(self, skill: str | None = None) -> list[dict]:
        with self._lock:
            return [r for r in self.records if skill is None or r["skill"] == skill]

    def forget(self, key: str) -> int:
        with self._lock:
            before = len(self.records)
            self.records = [r for r in self.records if r["key"] != key]
            self._save()
            return before - len(self.records)

    def log(self, event: str, **fields) -> None:
        line = {"at": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with self._lock, self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")

"""The tool contract. `Tools` is the only way Chief touches the outside world.

Three classes of tool:
  READ_TOOLS        run freely.
  SAFE_WRITE_TOOLS  side effects that cannot hurt (a draft, a Slack note).
  WRITE_TOOLS       change your calendar or send mail: only the approval gate may run these.
"""

from __future__ import annotations

from datetime import datetime

from chief.models import Email, Event, SleepDay

READ_TOOLS = {"list_events", "find_free_slots", "fetch_emails", "fetch_thread", "search_web", "read_health", "list_folder"}
SAFE_WRITE_TOOLS = {"create_draft", "post_slack", "open_folder", "open_file"}  # the last two only open things on screen
WRITE_TOOLS = {"patch_event", "create_event", "delete_event", "send_email"}
ALL_TOOLS = READ_TOOLS | SAFE_WRITE_TOOLS | WRITE_TOOLS


class Tools:
    """Base class. MockTools and LiveTools override these; unimplemented ones fail loudly."""

    def _todo(self, name: str):
        raise NotImplementedError(f"{type(self).__name__}.{name} is not implemented yet")

    # --- reads
    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        self._todo("list_events")

    def find_free_slots(self, start: datetime, end: datetime, minutes: int) -> list[tuple[datetime, datetime]]:
        self._todo("find_free_slots")

    def fetch_emails(self, query: str = "", max_results: int = 10) -> list[Email]:
        self._todo("fetch_emails")

    def fetch_thread(self, thread_id: str) -> list[Email]:
        self._todo("fetch_thread")

    def search_web(self, objective: str, queries: list[str], max_results: int = 5) -> list[dict]:
        """Returns [{"title", "url", "excerpts": [str]}]."""
        self._todo("search_web")

    def read_health(self, days: int = 14) -> list[SleepDay]:
        self._todo("read_health")

    def list_folder(self, path: str) -> dict:
        """Names and kinds inside an allow-listed folder, never contents. See chief/tools/local.py."""
        self._todo("list_folder")

    # --- safe writes
    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        self._todo("create_draft")

    def post_slack(self, text: str) -> dict:
        self._todo("post_slack")

    def open_folder(self, path: str) -> dict:
        """UI only: show an allow-listed folder in the file manager. See chief/tools/local.py."""
        self._todo("open_folder")

    def open_file(self, path: str) -> dict:
        """UI only: open an allow-listed document of a safe type. See chief/tools/local.py."""
        self._todo("open_file")

    # --- approval-only writes
    def patch_event(self, event_id: str, fields: dict) -> dict:
        self._todo("patch_event")

    def create_event(self, title: str, start: str, end: str, description: str = "") -> dict:
        self._todo("create_event")

    def delete_event(self, event_id: str) -> dict:
        self._todo("delete_event")

    def send_email(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        self._todo("send_email")

    def call(self, name: str, args: dict):
        if name not in ALL_TOOLS:
            raise ValueError(f"unknown tool: {name}")
        return getattr(self, name)(**args)


class ScopedTools:
    """What a skill sees while it runs: only its allowed read tools, never a write."""

    def __init__(self, tools: Tools, allowed: set[str], skill: str):
        self._tools, self._allowed, self._skill = tools, set(allowed), skill

    def __getattr__(self, name: str):
        if name in WRITE_TOOLS:
            raise PermissionError(f"skill '{self._skill}' cannot call {name} directly; propose it instead")
        if name not in ALL_TOOLS or name not in self._allowed:
            raise PermissionError(f"skill '{self._skill}' is not allowed to use {name}")
        return getattr(self._tools, name)

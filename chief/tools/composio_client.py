"""Thin wrapper around the Composio session API, plus the connect-link helper.

What was verified (against the `composio` 0.28.0 package source, installed in a scratch folder, and the
docs at docs.composio.dev/docs/configuring-sessions):
  - `Composio(api_key=...)`, `composio.sessions.create(user_id=, toolkits=[...], tools={toolkit: {"enable": [...]}})`
  - `session.execute(slug, arguments={...})` returns an object with `.data`, `.error`, `.result_type`
    ("completed" | "failed"); it does not retry, so a write is never silently repeated.
  - `session.authorize(toolkit)` returns a connection request with `.redirect_url`;
    `session.toolkits(toolkits=[...])` reports `.connection.is_active`.
What is NOT verified (no COMPOSIO_API_KEY was available): the inner shape of `.data` for each tool.
`unwrap` therefore accepts either an object or a dict, and the parsers in live.py search a few nesting levels.

CLI:  python -m chief.tools.composio_client gmail            (prints the link to open in a browser)
      python -m chief.tools.composio_client --status         (which toolkits are connected)
`python -m chief connect gmail` needs a two-line hook in chief/__main__.py that calls `connect_main(argv[1:])`
(see docs/live-tools.md); this module deliberately does not edit that file.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Callable

TOOLKIT_ALIASES = {"gmail": "gmail", "calendar": "googlecalendar", "googlecalendar": "googlecalendar"}


class LiveToolError(RuntimeError):
    """A Composio/Parallel call failed. The message names the tool slug so the activity log is readable."""


def get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access, so SDK objects and plain dicts (tests, older SDKs) look the same."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def unwrap(slug: str, resp: Any) -> Any:
    """Return the tool's data, or raise LiveToolError if Composio reports a failure."""
    failed = get(resp, "result_type") == "failed" or get(resp, "successful") is False
    error = get(resp, "error")
    if failed or error:
        raise LiveToolError(f"{slug} failed: {error or 'no error message'}")
    return get(resp, "data")


class ComposioGateway:
    """Lazily creates Composio sessions and runs tools. Two sessions are used on purpose:

    `read`  can only run read tools and drafts. Nothing in it can change the calendar or send mail.
    `write` is created only when an approved write actually runs.
    That way a bug in a read path cannot reach a write tool, even before the approval gate looks at it.
    """

    def __init__(
        self,
        user_id: str | None = None,
        api_key: str | None = None,
        composio: Any = None,
        read_tools: dict[str, list[str]] | None = None,
        write_tools: dict[str, list[str]] | None = None,
    ):
        self.user_id = user_id or os.getenv("CHIEF_USER_ID") or "chief-demo"
        self._api_key = api_key or os.getenv("COMPOSIO_API_KEY")
        self._composio = composio
        self._allowed = {"read": read_tools or {}, "write": write_tools or {}}
        self._sessions: dict[str, Any] = {}

    @property
    def composio(self) -> Any:
        if self._composio is None:
            if not self._api_key:
                raise LiveToolError("COMPOSIO_API_KEY is not set (put it in .env); live calendar and Gmail need it")
            from composio import Composio  # lazy: mock mode needs no extras

            self._composio = Composio(api_key=self._api_key)
        return self._composio

    def session(self, kind: str) -> Any:
        if kind not in self._sessions:
            tools = self._allowed[kind]
            self._sessions[kind] = self.composio.sessions.create(
                user_id=self.user_id,
                toolkits=sorted(tools),
                tools={toolkit: {"enable": list(slugs)} for toolkit, slugs in tools.items()},
            )
        return self._sessions[kind]

    def execute(self, kind: str, slug: str, arguments: dict) -> Any:
        """Run one tool in the `read` or `write` session and return its unwrapped data."""
        if not any(slug in slugs for slugs in self._allowed[kind].values()):
            raise LiveToolError(f"{slug} is not enabled for the {kind} session")
        args = {k: v for k, v in arguments.items() if v is not None}
        return unwrap(slug, self.session(kind).execute(slug, arguments=args))

    def connect_link(self, toolkit: str) -> str:
        """The URL the user opens once to connect (or reconnect) a Google account."""
        toolkit = TOOLKIT_ALIASES.get(toolkit.lower(), toolkit.lower())
        session = self.composio.sessions.create(user_id=self.user_id, toolkits=[toolkit])
        return session.authorize(toolkit).redirect_url

    def connection_status(self, toolkits: list[str] | None = None) -> dict[str, bool]:
        names = [TOOLKIT_ALIASES.get(t.lower(), t.lower()) for t in (toolkits or ["gmail", "googlecalendar"])]
        session = self.composio.sessions.create(user_id=self.user_id, toolkits=names)
        found = session.toolkits(toolkits=names)
        out = {n: False for n in names}
        for item in get(found, "items", []) or []:
            conn = get(item, "connection")
            out[get(item, "slug")] = bool(conn and get(conn, "is_active"))
        return out


def connect_main(argv: list[str] | None = None, gateway_factory: Callable[[], ComposioGateway] = ComposioGateway) -> int:
    """`python -m chief connect gmail` hands everything after `connect` to this function."""
    ap = argparse.ArgumentParser(prog="chief connect")
    ap.add_argument("toolkit", nargs="?", choices=sorted(TOOLKIT_ALIASES), help="gmail or calendar")
    ap.add_argument("--status", action="store_true", help="show which toolkits are connected instead")
    args = ap.parse_args(argv)
    if not args.status and not args.toolkit:
        ap.error("give a toolkit (gmail or calendar), or --status")
    try:
        gateway = gateway_factory()
        if args.status:
            for name, ok in gateway.connection_status().items():
                print(f"{name:16} {'connected' if ok else 'NOT connected (run: connect ' + name + ')'}")
        else:
            print(f"Open this link in a browser signed in to the Google test account:\n{gateway.connect_link(args.toolkit)}")
    except LiveToolError as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(connect_main())

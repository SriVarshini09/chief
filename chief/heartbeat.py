"""The scheduled check-in loop."""

from __future__ import annotations

import asyncio
import logging

from chief.runtime import Runtime

log = logging.getLogger("chief.heartbeat")


async def run_forever(rt: Runtime) -> None:
    """Tick every `heartbeat_hours`. A tick that finds nothing says nothing."""
    while True:
        try:
            proposals = rt.tick()
            log.info("heartbeat: %d new proposal(s)", len(proposals))
        except Exception:  # a bad skill must not stop the loop
            log.exception("heartbeat tick failed")
        await asyncio.sleep(rt.cfg.heartbeat_hours * 3600)

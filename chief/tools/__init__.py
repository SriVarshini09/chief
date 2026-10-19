from __future__ import annotations

from chief.config import Config
from chief.tools.base import ALL_TOOLS, READ_TOOLS, SAFE_WRITE_TOOLS, WRITE_TOOLS, ScopedTools, Tools

__all__ = ["ALL_TOOLS", "READ_TOOLS", "SAFE_WRITE_TOOLS", "WRITE_TOOLS", "ScopedTools", "Tools", "get_tools"]


def get_tools(cfg: Config) -> Tools:
    """mock = seeded data. live = real services. demo = live with failover to the recorded cache (reads only).
    demo-offline = no network: cached reads, writes simulated by MockTools and flagged."""
    if cfg.mode == "mock":
        from chief.tools.mock import MockTools

        return MockTools(cfg.now)
    if cfg.mode in ("demo", "demo-offline"):
        from chief.tools.replay import DemoCache, FailoverTools, ReplayTools

        cache = DemoCache.load(cfg.cache_path)
        if cfg.mode == "demo-offline":
            from chief.tools.mock import MockTools

            return ReplayTools(cache, writes=MockTools(cfg.now), reason="offline demo")
        from chief.tools.live import LiveTools

        return FailoverTools(LiveTools(cfg), ReplayTools(cache), timeout=cfg.live_timeout)
    from chief.tools.live import LiveTools

    return LiveTools(cfg)

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:  # optional: .env support
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _seconds(value: str | None, default: float) -> float:
    try:
        return max(0.5, float(value)) if value else default
    except ValueError:
        return default


@dataclass
class Config:
    mode: str = "mock"  # mock | live | demo (live with cached failover) | demo-offline (cache + simulated writes)
    data_dir: Path = ROOT / "data"
    state_dir: Path = ROOT / ".chief"
    heartbeat_hours: float = 4.0
    active_start: int = 8  # local hour, inclusive
    active_end: int = 22  # local hour, exclusive
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    now: datetime | None = None  # pin "now" for deterministic demos and tests
    # real-window layer (chief/screen.py), off by default
    show_windows: bool = False
    show_profile_dir: Path = ROOT / ".chief" / "browser-profile"
    show_browser: str | None = None
    show_window_size: str | None = None  # "1280x800"
    show_window_pos: str | None = None  # "1920,0" puts windows on a monitor to the right
    show_debug_port: int | None = None  # optional: reload Calendar in place via DevTools
    show_dry_run: bool = False  # print the URLs instead of opening a browser
    local_roots: tuple[str, ...] = ()  # folders Chief may list/open (CHIEF_LOCAL_ROOTS); empty = the defaults, see chief/tools/local.py
    live_timeout: float = 6.0  # demo mode: seconds a live read may take before the cached answer is used
    cache_file: Path | None = None  # default: data_dir/demo_cache.json

    @property
    def cache_path(self) -> Path:
        return self.cache_file or self.data_dir / "demo_cache.json"

    @classmethod
    def from_env(cls) -> "Config":
        pinned = os.getenv("CHIEF_NOW")
        state_dir = Path(os.getenv("CHIEF_STATE_DIR") or ROOT / ".chief")
        port = os.getenv("CHIEF_SHOW_DEBUG_PORT", "").strip()
        return cls(
            mode=os.getenv("CHIEF_MODE", "mock"),
            state_dir=state_dir,
            show_windows=_flag("CHIEF_SHOW_WINDOWS"),
            show_profile_dir=Path(os.getenv("CHIEF_SHOW_PROFILE_DIR") or state_dir / "browser-profile"),
            show_browser=os.getenv("CHIEF_SHOW_BROWSER") or None,
            show_window_size=os.getenv("CHIEF_SHOW_WINDOW_SIZE") or None,
            show_window_pos=os.getenv("CHIEF_SHOW_WINDOW_POS") or None,
            show_debug_port=int(port) if port.isdigit() else None,
            show_dry_run=_flag("CHIEF_SHOW_DRY_RUN"),
            local_roots=tuple(r.strip().strip('"') for r in re.split(r"[;\r\n]+", os.getenv("CHIEF_LOCAL_ROOTS", "")) if r.strip().strip('"')),
            live_timeout=_seconds(os.getenv("CHIEF_LIVE_TIMEOUT"), 6.0),
            cache_file=Path(os.environ["CHIEF_DEMO_CACHE"]) if os.getenv("CHIEF_DEMO_CACHE") else None,
            now=datetime.fromisoformat(pinned) if pinned else None,
            llm_base_url=os.getenv("CHIEF_LLM_BASE_URL") or None,
            llm_model=os.getenv("CHIEF_LLM_MODEL") or None,
            llm_api_key=os.getenv("CHIEF_LLM_API_KEY") or None,
        )

    def in_active_hours(self, now: datetime) -> bool:
        return self.active_start <= now.hour < self.active_end

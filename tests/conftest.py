from __future__ import annotations

from datetime import datetime

import pytest

from chief.config import Config
from chief.runtime import Runtime
from chief.tools.mock import MockTools

NOW = datetime(2026, 10, 21, 8, 0)  # a Wednesday morning; next Thursday is a heavy day


@pytest.fixture
def cfg(tmp_path):
    return Config(mode="mock", state_dir=tmp_path / "state", now=NOW)


@pytest.fixture
def tools():
    return MockTools(NOW)


@pytest.fixture
def cards():
    return []


@pytest.fixture
def rt(cfg, tools, cards):
    return Runtime(cfg, tools=tools, notify=cards.append)

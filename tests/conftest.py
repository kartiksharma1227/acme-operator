"""Shared fixtures: a run context for tool tests, and a real sandbox server on a free port."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import load_config
from agent.human import ScriptedChannel
from agent.models import RunState
from agent.tools import RunContext, build_registry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def events():
    return []


@pytest.fixture
def ctx(tmp_path, events):
    config = load_config()
    config.headless = True
    context = RunContext(
        config=config,
        state=RunState(run_id="test"),
        run_dir=tmp_path / "run",
        human=ScriptedChannel(approvals={"*": "approve"}, answers=["42"]),
        emit=lambda event_type, data: events.append((event_type, data)),
    )
    yield context
    context.close()


@pytest.fixture
def registry():
    return build_registry()

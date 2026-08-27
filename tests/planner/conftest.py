"""Shared planner test harness: fakes wired through the real ports.

Reuses the orchestrator harness pieces (clock, workspaces) and the dual
runner fakes, so the planner suite proves the same runner-agnosticism the
orchestrator suite does (ADR-0011).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakeCopilotRunner, FakeRunner
from tests.orchestrator.conftest import FakeClock, FakeWorkspaces
from ticketflow.config import CodeHostConfig, Config, PlannerConfig, TrackerConfig
from ticketflow.store.store import Store

PLAN_ID = "a3f8c2d91b04"


@pytest.fixture(params=[FakeRunner, FakeCopilotRunner], ids=["claude-like", "copilot-like"])
def runner(request: pytest.FixtureRequest) -> FakeRunner:
    runner: FakeRunner = request.param()
    return runner


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "ticketflow.db")
    yield s
    s.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def workspaces(tmp_path: Path) -> FakeWorkspaces:
    return FakeWorkspaces(tmp_path / "ws")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        tracker=TrackerConfig(provider="github", repo="u-ways/qa"),
        codehost=CodeHostConfig(repo="u-ways/qa"),
        planner=PlannerConfig(
            synthesis_model="test-model",
            grounding_timeout_seconds=30,
            poll_interval_seconds=1.0,
        ),
        state_dir=tmp_path / ".ticketflow",
        plans_dir=tmp_path / "plans",
    )

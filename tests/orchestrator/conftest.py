"""Shared orchestrator test harness: fakes wired through the real ports."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakeCodeHost, FakeCopilotRunner, FakeRunner, FakeTracker
from ticketflow.config import CodeHostConfig, Config, Limits, TrackerConfig
from ticketflow.domain.model import NodeState
from ticketflow.orchestrator.core import Orchestrator
from ticketflow.ports.codehost import (
    CheckConclusion,
    CheckState,
    PrStatus,
    ReviewDecision,
)
from ticketflow.ports.tracker import TrackerItem
from ticketflow.store.store import Store

T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Monotonic fake clock; advances one second per call."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class FakeWorkspaces:
    """Workspace provider handing out per-attempt tmp directories."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bootstrap_requests: list[tuple[str, int]] = []

    def prepare(self, node_id: str, attempt: int, *, bootstrap: bool) -> Path:
        if bootstrap:
            self.bootstrap_requests.append((node_id, attempt))
        path = self.root / node_id / str(attempt)
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass
class Harness:
    store: Store
    tracker: FakeTracker
    runner: FakeRunner
    codehost: FakeCodeHost
    workspaces: FakeWorkspaces
    clock: FakeClock
    orchestrator: Orchestrator
    config: Config

    items: list[TrackerItem] = field(default_factory=list)

    def add_item(self, key: str, title: str, body: str = "", closed: bool = False) -> None:
        self.tracker.items.append(
            TrackerItem(provider="github", external_key=key, title=title, body=body, closed=closed)
        )

    def node_id_for(self, key: str) -> str:
        node_id = self.store.resolve_external("github", key)
        assert node_id is not None, f"no node synced for {key}"
        return node_id

    def state_of(self, key: str) -> NodeState:
        node = self.store.get_node(self.node_id_for(key))
        assert node is not None
        return node.state

    def set_pr(
        self,
        pr_number: int,
        *,
        checks: dict[str, CheckState] | None = None,
        decision: ReviewDecision = ReviewDecision.NONE,
        threads: int = 0,
        state: str = "open",
    ) -> None:
        from tests.fakes import FakePr

        status = PrStatus(
            number=pr_number,
            state=state,
            checks=tuple(CheckConclusion(name=n, state=s) for n, s in (checks or {}).items()),
            review_decision=decision,
            unresolved_threads=threads,
        )
        if pr_number in self.codehost.prs:
            self.codehost.prs[pr_number].status = status
        else:
            self.codehost.prs[pr_number] = FakePr(status=status)


@pytest.fixture(params=[FakeRunner, FakeCopilotRunner], ids=["claude-like", "copilot-like"])
def h(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Harness]:
    # Both runner fakes drive the same suite so the port stays
    # runner-agnostic (ADR-0011).
    store = Store.open(tmp_path / "tf.db")
    tracker = FakeTracker()
    runner = request.param()
    codehost = FakeCodeHost()
    workspaces = FakeWorkspaces(tmp_path / "ws")
    clock = FakeClock()
    config = Config(
        tracker=TrackerConfig(provider="github", repo="u-ways/qa"),
        codehost=CodeHostConfig(repo="u-ways/qa"),
        limits=Limits(max_parallel=2, max_attempts=3, cycle_cap=100, halt_ticks=3),
        state_dir=tmp_path / ".ticketflow",
    )
    orchestrator = Orchestrator(
        store=store,
        tracker=tracker,
        runner=runner,
        codehost=codehost,
        workspaces=workspaces,
        config=config,
        clock=clock,
    )
    yield Harness(
        store=store,
        tracker=tracker,
        runner=runner,
        codehost=codehost,
        workspaces=workspaces,
        clock=clock,
        orchestrator=orchestrator,
        config=config,
    )
    store.close()

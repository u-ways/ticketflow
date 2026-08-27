"""Grounding (ADR-0014): a RunnerPort attempt whose brief is captured from
``brief.md`` at the workspace root; runaway-guarded, resumable, evented."""

from pathlib import Path

import pytest

from tests.fakes import FakeRunner
from tests.orchestrator.conftest import FakeClock, FakeWorkspaces
from ticketflow.config import Config
from ticketflow.domain.errors import GroundingFailed
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.planner.grounding import grounding_workspace_id, run_grounding
from ticketflow.ports.runner import NodeDispatch, RunnerHandle, ToolPolicy
from ticketflow.store.store import Store

from .conftest import PLAN_ID


def make_plan(store: Store, clock: FakeClock) -> PlanRecord:
    store.create_plan(plan_id=PLAN_ID, provider="github", epic_key="#42", now=clock())
    plan = store.get_plan(PLAN_ID)
    assert plan is not None
    return plan


def write_brief(workspaces: FakeWorkspaces, attempt: int = 1, text: str = "# Brief\n") -> None:
    path = workspaces.root / grounding_workspace_id(PLAN_ID) / str(attempt)
    path.mkdir(parents=True, exist_ok=True)
    (path / "brief.md").write_text(text, encoding="utf-8")


def ground(
    store: Store,
    runner: FakeRunner,
    workspaces: FakeWorkspaces,
    config: Config,
    clock: FakeClock,
    *,
    greenfield: bool = False,
    yolo: bool = False,
) -> str:
    plan = store.get_plan(PLAN_ID)
    assert plan is not None
    return run_grounding(
        store=store,
        runner=runner,
        workspaces=workspaces,
        config=config,
        plan=plan,
        prompt="research the epic",
        greenfield=greenfield,
        yolo=yolo,
        clock=clock,
        sleep=lambda seconds: clock.advance(int(seconds)),
    )


class TestSuccess:
    def test_brief_captured_and_status_advances(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        make_plan(store, clock)
        write_brief(workspaces, text="# Brief\nFindings.\n")
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1)

        brief = ground(store, runner, workspaces, config, clock)

        assert brief == "# Brief\nFindings.\n"
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.status is PlanStatus.SYNTHESIS
        assert plan.brief == "# Brief\nFindings.\n"
        assert plan.grounding_attempts == 1
        assert plan.grounding_pid is not None
        assert plan.session_id == f"{runner.SESSION_PREFIX}-1"
        kinds = [e.kind for e in store.events_after(0)]
        assert "plan_grounding_dispatched" in kinds
        assert "plan_grounded" in kinds

    def test_tool_policy_and_model_come_from_config(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        config.planner.grounding_model = "grounding-model"
        make_plan(store, clock)
        write_brief(workspaces)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1)

        ground(store, runner, workspaces, config, clock)

        started = runner.started[0]
        assert started.policy.allowed_tools == ("Read", "Grep", "Glob")
        assert started.policy.yolo is False
        assert started.dispatch.model == "grounding-model"

    def test_model_falls_back_to_runner_config(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        config.runner.model = "runner-model"
        make_plan(store, clock)
        write_brief(workspaces)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1)
        ground(store, runner, workspaces, config, clock)
        assert runner.started[0].dispatch.model == "runner-model"

    def test_greenfield_uses_bootstrap_workspace(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        make_plan(store, clock)
        write_brief(workspaces)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1)
        ground(store, runner, workspaces, config, clock, greenfield=True)
        assert workspaces.bootstrap_requests == [(grounding_workspace_id(PLAN_ID), 1)]

    def test_yolo_flows_into_the_policy(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        make_plan(store, clock)
        write_brief(workspaces)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1)
        ground(store, runner, workspaces, config, clock, yolo=True)
        assert runner.started[0].policy.yolo is True


class TestFailures:
    def test_nonzero_exit_fails_and_stays_grounding(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        make_plan(store, clock)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1, exit_code=2)

        with pytest.raises(GroundingFailed, match="exited 2"):
            ground(store, runner, workspaces, config, clock)

        assert store.plan_status(PLAN_ID) is PlanStatus.GROUNDING
        kinds = [e.kind for e in store.events_after(0)]
        assert "plan_grounding_failed" in kinds

    def test_clean_exit_without_brief_fails(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        make_plan(store, clock)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1)
        with pytest.raises(GroundingFailed, match=r"brief\.md"):
            ground(store, runner, workspaces, config, clock)
        assert store.plan_status(PLAN_ID) is PlanStatus.GROUNDING

    def test_wall_clock_guard_cancels_a_stuck_agent(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        config.planner.grounding_timeout_seconds = 5
        make_plan(store, clock)
        # No scripted outcome: the fake reports RUNNING forever.
        with pytest.raises(GroundingFailed, match="wall-clock"):
            ground(store, runner, workspaces, config, clock)
        assert len(runner.cancelled) == 1
        assert store.plan_status(PLAN_ID) is PlanStatus.GROUNDING

    def test_crash_between_dispatch_and_bookkeeping_never_reuses_the_attempt(
        self,
        store: Store,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        # The attempt number is claimed before runner.start, so an orphan
        # from a crash mid-turn is superseded, never collided with.
        class ExplodingRunner(FakeRunner):
            def start(
                self, node: NodeDispatch, workspace: Path, policy: ToolPolicy
            ) -> RunnerHandle:
                del node, workspace, policy
                raise RuntimeError("crash after the agent was spawned")

        make_plan(store, clock)
        with pytest.raises(RuntimeError):
            ground(store, ExplodingRunner(), workspaces, config, clock)
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.grounding_attempts == 1  # the number was consumed

        healthy = FakeRunner()
        write_brief(workspaces, attempt=2)
        healthy.script_exit(grounding_workspace_id(PLAN_ID), 2)
        ground(store, healthy, workspaces, config, clock)
        assert healthy.started[0].dispatch.attempt == 2  # fresh dirs, no reuse

    def test_retry_after_failure_is_attempt_two(
        self,
        store: Store,
        runner: FakeRunner,
        workspaces: FakeWorkspaces,
        config: Config,
        clock: FakeClock,
    ) -> None:
        make_plan(store, clock)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 1, exit_code=1)
        with pytest.raises(GroundingFailed):
            ground(store, runner, workspaces, config, clock)

        write_brief(workspaces, attempt=2)
        runner.script_exit(grounding_workspace_id(PLAN_ID), 2)
        ground(store, runner, workspaces, config, clock)

        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.grounding_attempts == 2
        assert plan.status is PlanStatus.SYNTHESIS
        assert Path(runner.started[1].dispatch.run_dir).name == "2"

"""The grounding phase (ADR-0014): a tool-using agent via the RunnerPort.

The agent explores the repo and tracker context in a detached attempt and
writes ``brief.md`` at its workspace root — the same file-capture pattern as
handoffs, because ``PollResult`` carries no text. The foreground turn polls
until exit, bounded by the grounding wall-clock runaway guard (ADR-0010; the
runner's own per-attempt guards apply underneath).

Crash recovery is re-running the turn. The attempt number is claimed
BEFORE dispatch — monotonic, like node attempts (ADR-0008) — so a crash
between dispatch and bookkeeping can never make the re-run share a live
orphan's run dir or workspace: the orphan is superseded, keeps its own
directories, and dies to its runner-level runaway guards; the recorded
``(pid, create_time)`` identifies it (ADR-0003: nothing lives only in
process memory).
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from ticketflow.config import Config
from ticketflow.domain.errors import GroundingFailed
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.ports.runner import (
    AttemptStatus,
    NodeDispatch,
    RunnerHandle,
    RunnerPort,
    ToolPolicy,
)
from ticketflow.store.store import Store
from ticketflow.supervision.process import is_alive


class WorkspaceProvider(Protocol):
    """The slice of the workspace provider grounding needs (structural)."""

    def prepare(self, node_id: str, attempt: int, *, bootstrap: bool) -> Path: ...


def grounding_workspace_id(plan_id: str) -> str:
    """Run-dir and workspace identity for a plan's grounding attempts."""
    return f"plan-{plan_id}"


def run_grounding(
    *,
    store: Store,
    runner: RunnerPort,
    workspaces: WorkspaceProvider,
    config: Config,
    plan: PlanRecord,
    prompt: str,
    greenfield: bool,
    yolo: bool,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> str:
    """Dispatch one grounding attempt and wait for its brief.

    Returns the brief text; raises :class:`GroundingFailed` when the agent
    crashes, times out, or exits without writing ``brief.md``.
    """
    pseudo_id = grounding_workspace_id(plan.plan_id)
    _supersede_live_orphan(store, runner, plan, pseudo_id, clock)
    if plan.status is PlanStatus.INGESTED:
        store.set_plan_status(plan.plan_id, PlanStatus.GROUNDING, now=clock())
    # Claim the attempt number before anything is dispatched (monotonic,
    # ADR-0008): a crash after runner.start can only ever be superseded,
    # never collided with.
    attempt = store.bump_grounding_attempt(plan.plan_id, now=clock())
    run_dir = config.runs_dir / pseudo_id / str(attempt)
    workspace = workspaces.prepare(pseudo_id, attempt, bootstrap=greenfield)
    policy = ToolPolicy(
        allowed_tools=config.planner.grounding_allowed_tools,
        disallowed_tools=config.planner.grounding_disallowed_tools,
        yolo=yolo,
    )
    dispatch = NodeDispatch(
        node_id=pseudo_id,
        attempt=attempt,
        prompt=prompt,
        run_dir=run_dir,
        model=config.planner.grounding_model or config.runner.model,
    )

    handle = runner.start(dispatch, workspace, policy)
    store.set_grounding_process(
        plan.plan_id, pid=handle.pid, create_time=handle.create_time, now=clock()
    )
    store.append_event(
        "plan_grounding_dispatched",
        now=clock(),
        payload={"plan_id": plan.plan_id, "attempt": attempt, "pid": handle.pid},
    )

    deadline = clock() + timedelta(seconds=config.planner.grounding_timeout_seconds)
    try:
        while True:
            result = runner.poll(handle)
            if result.status is AttemptStatus.RUNNING:
                if clock() >= deadline:
                    runner.cancel(handle)
                    raise _failed(store, plan, attempt, "grounding wall-clock guard fired", clock())
                sleep(config.planner.poll_interval_seconds)
                continue
            if result.status is AttemptStatus.TIMED_OUT:
                runner.cancel(handle)
                reason = result.guard_reason or "runner runaway guard fired"
                raise _failed(store, plan, attempt, reason, clock())
            break
    except KeyboardInterrupt:
        # The turn is foreground; an interrupt must not orphan the detached
        # agent — a re-run would supersede it anyway, so end it now.
        runner.cancel(handle)
        raise

    if result.session_id is not None:
        store.set_plan_session(plan.plan_id, result.session_id, now=clock())
    if result.exit_code != 0:
        raise _failed(store, plan, attempt, f"grounding agent exited {result.exit_code}", clock())

    brief_path = workspace / "brief.md"
    if not brief_path.is_file():
        raise _failed(store, plan, attempt, "agent exited clean but wrote no brief.md", clock())
    brief = brief_path.read_text(encoding="utf-8")

    store.set_plan_brief(plan.plan_id, brief, now=clock())
    store.set_plan_status(plan.plan_id, PlanStatus.SYNTHESIS, now=clock())
    store.append_event(
        "plan_grounded",
        now=clock(),
        payload={"plan_id": plan.plan_id, "attempt": attempt, "brief_chars": len(brief)},
    )
    return brief


def _supersede_live_orphan(
    store: Store,
    runner: RunnerPort,
    plan: PlanRecord,
    pseudo_id: str,
    clock: Callable[[], datetime],
) -> None:
    """Cancel a previous turn's still-running agent before redispatching.

    Grounding turns are exclusive: the orphan's brief will never be read, and
    the new attempt's workspace prepare would otherwise retire a live
    process's worktree (ADR-0010's supersede, made explicit)."""
    if plan.grounding_pid is None or plan.grounding_create_time is None:
        return
    if not is_alive(plan.grounding_pid, plan.grounding_create_time):
        return
    runner.cancel(
        RunnerHandle(
            node_id=pseudo_id,
            attempt=plan.grounding_attempts,
            pid=plan.grounding_pid,
            create_time=plan.grounding_create_time,
            run_dir=Path(),
        )
    )
    store.append_event(
        "plan_grounding_superseded",
        now=clock(),
        payload={"plan_id": plan.plan_id, "pid": plan.grounding_pid},
    )


def _failed(
    store: Store, plan: PlanRecord, attempt: int, reason: str, now: datetime
) -> GroundingFailed:
    store.append_event(
        "plan_grounding_failed",
        now=now,
        payload={"plan_id": plan.plan_id, "attempt": attempt, "reason": reason},
    )
    return GroundingFailed(f"plan {plan.plan_id}: {reason}")

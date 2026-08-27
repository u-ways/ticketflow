"""In-memory fakes implementing the port interfaces (ADR-0002).

Core and orchestrator tests use these through the same interfaces the real
adapters implement — vendor SDKs are never mocked in core tests.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ticketflow.domain.model import NodeState
from ticketflow.planner.schema import Plan
from ticketflow.planner.synthesis import RevisionRequest, SynthesisRequest
from ticketflow.ports.codehost import (
    CheckConclusion,
    CheckState,
    PrStatus,
    ReviewComment,
    ReviewDecision,
)
from ticketflow.ports.runner import (
    AttemptStatus,
    NodeDispatch,
    PollResult,
    RunnerCapabilities,
    RunnerHandle,
    ToolPolicy,
)
from ticketflow.ports.tracker import (
    TrackerCapabilities,
    TrackerIntent,
    TrackerItem,
)


class FakeTracker:
    """Scriptable tracker: seed items/intents, observe pushes and emission.

    Created items land in ``self.items`` with sequential keys, so a
    subsequent ``fetch_nodes`` returns them — emitted children enter the
    core as ordinary synced tracker items (ADR-0014). The ``fail_*`` knobs
    script emission failures for the crash-recovery walkthroughs.
    """

    def __init__(self) -> None:
        self.items: list[TrackerItem] = []
        self.intents: list[TrackerIntent] = []
        self.pushed_states: list[tuple[str, NodeState]] = []
        self.comments: list[tuple[str, str]] = []
        self.created: list[tuple[str, str, tuple[str, ...], str | None]] = []
        """(external_key, body, labels, parent_key) per create_item call."""
        self.body_updates: list[tuple[str, str]] = []
        self.mirrored: list[tuple[str, tuple[str, ...]]] = []
        self.fail_after_creates: int | None = None
        """Raise on create_item once this many items exist (None = never)."""
        self.fail_after_body_updates: int | None = None
        self.fail_mirror: bool = False
        self.record_created_items: bool = True
        """False scripts the created-but-unrecorded crash window: the ticket
        exists on the tracker but create_item dies before returning."""
        self._next_number = 100

    def fetch_nodes(self, cursor: str | None) -> tuple[list[TrackerItem], str | None]:
        del cursor
        return list(self.items), "cursor-1"

    def fetch_intents(self, cursor: str | None) -> tuple[list[TrackerIntent], str | None]:
        del cursor
        return list(self.intents), "cursor-1"

    def push_state(self, external_key: str, state: NodeState) -> None:
        self.pushed_states.append((external_key, state))

    def push_comment(self, external_key: str, text: str) -> None:
        self.comments.append((external_key, text))

    def create_item(
        self,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        parent_key: str | None = None,
    ) -> str:
        if self.fail_after_creates is not None and len(self.created) >= self.fail_after_creates:
            raise RuntimeError("scripted tracker failure on create_item")
        self._next_number += 1
        key = f"#{self._next_number}"
        self.items.append(TrackerItem(provider="github", external_key=key, title=title, body=body))
        if not self.record_created_items:
            raise RuntimeError("scripted crash after the tracker created the item")
        self.created.append((key, body, labels, parent_key))
        return key

    def update_body(self, external_key: str, body: str) -> None:
        if (
            self.fail_after_body_updates is not None
            and len(self.body_updates) >= self.fail_after_body_updates
        ):
            raise RuntimeError("scripted tracker failure on update_body")
        for i, item in enumerate(self.items):
            if item.external_key == external_key:
                self.items[i] = TrackerItem(
                    provider=item.provider,
                    external_key=item.external_key,
                    title=item.title,
                    body=body,
                    etag=item.etag,
                    closed=item.closed,
                    updated_at=item.updated_at,
                )
                break
        else:
            raise RuntimeError(f"update_body on unknown item {external_key}")
        self.body_updates.append((external_key, body))

    def mirror_dependencies(self, external_key: str, depends_on: tuple[str, ...]) -> None:
        if self.fail_mirror:
            raise RuntimeError("scripted mirror failure")
        self.mirrored.append((external_key, depends_on))

    def capabilities(self) -> TrackerCapabilities:
        return TrackerCapabilities()


@dataclass
class StartedAttempt:
    dispatch: NodeDispatch
    workspace: Path
    policy: ToolPolicy


class FakeRunner:
    """Scriptable Claude-shaped runner: outcomes queued per (node_id, attempt).

    A second, deliberately different fake (:class:`FakeCopilotRunner`) runs
    the same orchestrator suite so the port cannot drift into Claude-shaped
    assumptions (ADR-0011).
    """

    PID_BASE = 1000
    SESSION_PREFIX = "sess"

    def __init__(self) -> None:
        self.started: list[StartedAttempt] = []
        self.resumed: list[tuple[RunnerHandle, str]] = []
        self.cancelled: list[RunnerHandle] = []
        self._outcomes: dict[tuple[str, int], deque[PollResult]] = {}
        self._next_pid = self.PID_BASE

    def script(self, node_id: str, attempt: int, *results: PollResult) -> None:
        self._outcomes.setdefault((node_id, attempt), deque()).extend(results)

    def script_exit(self, node_id: str, attempt: int, exit_code: int = 0) -> None:
        self.script(
            node_id,
            attempt,
            PollResult(
                status=AttemptStatus.EXITED,
                exit_code=exit_code,
                session_id=f"{self.SESSION_PREFIX}-1",
            ),
        )

    def start(self, node: NodeDispatch, workspace: Path, policy: ToolPolicy) -> RunnerHandle:
        self.started.append(StartedAttempt(node, workspace, policy))
        self._next_pid += 1
        return RunnerHandle(
            node_id=node.node_id,
            attempt=node.attempt,
            pid=self._next_pid,
            create_time=float(self._next_pid),
            run_dir=node.run_dir,
            session_id=None,
        )

    def poll(self, handle: RunnerHandle) -> PollResult:
        queue = self._outcomes.get((handle.node_id, handle.attempt))
        if queue:
            return queue.popleft()
        return PollResult(status=AttemptStatus.RUNNING)

    def resume(self, handle: RunnerHandle, feedback: str) -> RunnerHandle:
        self.resumed.append((handle, feedback))
        self._next_pid += 1
        return RunnerHandle(
            node_id=handle.node_id,
            attempt=handle.attempt,
            pid=self._next_pid,
            create_time=float(self._next_pid),
            run_dir=handle.run_dir,
            session_id=handle.session_id,
        )

    def cancel(self, handle: RunnerHandle) -> None:
        self.cancelled.append(handle)

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(supports_resume=True)


class FakeCopilotRunner(FakeRunner):
    """A second runner fake with different vendor-flavoured details.

    Different pid range, session-id shape, and cost semantics (a prompt
    allowance normalized to a float at the boundary, ADR-0011). Exercised
    against the whole orchestrator suite alongside :class:`FakeRunner`.
    """

    PID_BASE = 20_000
    SESSION_PREFIX = "copilot-thread"

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(supports_resume=True, reports_cost=True)


@dataclass
class FakePr:
    status: PrStatus
    feedback: list[ReviewComment] = field(default_factory=list)


class FakeCodeHost:
    """Scriptable code host: set repo/branch/PR state, observe actions."""

    def __init__(self) -> None:
        self.exists = True
        self.default = "main"
        self.branches: set[str] = set()
        self.prs: dict[int, FakePr] = {}
        self.branch_prs: dict[str, int] = {}
        self.opened: list[tuple[str, str, str]] = []
        self.merged: list[int] = []
        self.auto_merged: list[int] = []
        self.reruns: list[int] = []
        self.comments: list[tuple[int, str]] = []
        self.resolved_threads: list[str] = []
        self.merge_result = True
        self.auto_merge_result = False
        self._next_pr = 100

    def repo_exists(self) -> bool:
        return self.exists

    def default_branch(self) -> str | None:
        return self.default if self.exists else None

    def branch_exists(self, branch: str) -> bool:
        return branch in self.branches

    def open_pr(self, branch: str, title: str, body: str) -> int:
        self.opened.append((branch, title, body))
        self._next_pr += 1
        self.branch_prs[branch] = self._next_pr
        # A fresh PR reports its checks as pending until the test scripts them.
        self.prs[self._next_pr] = FakePr(
            status=PrStatus(
                number=self._next_pr,
                state="open",
                checks=(CheckConclusion(name="setup", state=CheckState.PENDING),),
                review_decision=ReviewDecision.NONE,
                unresolved_threads=0,
            )
        )
        return self._next_pr

    def find_pr_for_branch(self, branch: str) -> int | None:
        return self.branch_prs.get(branch)

    def get_pr_status(self, pr_number: int) -> PrStatus:
        return self.prs[pr_number].status

    def get_feedback(self, pr_number: int, since: datetime | None) -> list[ReviewComment]:
        del since
        pr = self.prs.get(pr_number)
        return list(pr.feedback) if pr else []

    def resolve_thread(self, thread_id: str) -> None:
        self.resolved_threads.append(thread_id)

    def rerun_failed_checks(self, pr_number: int) -> bool:
        self.reruns.append(pr_number)
        return True

    def merge(self, pr_number: int) -> bool:
        if self.merge_result:
            self.merged.append(pr_number)
        return self.merge_result

    def enable_auto_merge(self, pr_number: int) -> bool:
        if self.auto_merge_result:
            self.auto_merged.append(pr_number)
        return self.auto_merge_result

    def post_comment(self, pr_number: int, text: str) -> None:
        self.comments.append((pr_number, text))


class FakeSynthesizer:
    """Scriptable PlanSynthesizer: queue Plan results or exceptions.

    Records every request so tests assert exactly what synthesis was a
    function of — the stateless-turn property of ADR-0014.
    """

    def __init__(self) -> None:
        self.requests: list[SynthesisRequest | RevisionRequest] = []
        self._results: deque[Plan | Exception] = deque()

    def script(self, *results: Plan | Exception) -> None:
        self._results.extend(results)

    def synthesize(self, request: SynthesisRequest) -> Plan:
        return self._next(request)

    def revise(self, request: RevisionRequest) -> Plan:
        return self._next(request)

    def _next(self, request: SynthesisRequest | RevisionRequest) -> Plan:
        self.requests.append(request)
        assert self._results, "FakeSynthesizer: no scripted result left"
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result

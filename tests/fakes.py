"""In-memory fakes implementing the port interfaces (ADR-0002).

Core and orchestrator tests use these through the same interfaces the real
adapters implement — vendor SDKs are never mocked in core tests.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ticketflow.domain.model import NodeState
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
    """Scriptable tracker: seed items/intents, observe pushes."""

    def __init__(self) -> None:
        self.items: list[TrackerItem] = []
        self.intents: list[TrackerIntent] = []
        self.pushed_states: list[tuple[str, NodeState]] = []
        self.comments: list[tuple[str, str]] = []

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

    def capabilities(self) -> TrackerCapabilities:
        return TrackerCapabilities()


@dataclass
class StartedAttempt:
    dispatch: NodeDispatch
    workspace: Path
    policy: ToolPolicy


class FakeRunner:
    """Scriptable runner: outcomes are queued per (node_id, attempt)."""

    def __init__(self) -> None:
        self.started: list[StartedAttempt] = []
        self.resumed: list[tuple[RunnerHandle, str]] = []
        self.cancelled: list[RunnerHandle] = []
        self._outcomes: dict[tuple[str, int], deque[PollResult]] = {}
        self._next_pid = 1000

    def script(self, node_id: str, attempt: int, *results: PollResult) -> None:
        self._outcomes.setdefault((node_id, attempt), deque()).extend(results)

    def script_exit(self, node_id: str, attempt: int, exit_code: int = 0) -> None:
        self.script(
            node_id,
            attempt,
            PollResult(status=AttemptStatus.EXITED, exit_code=exit_code, session_id="sess-1"),
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

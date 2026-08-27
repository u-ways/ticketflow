"""Runner port (ADR-0002, ADR-0011, spec §7.2).

Async-shaped deliberately — start returns a handle that is polled — so a
remote assign-issue-and-wait runner fits later without reshaping the core.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Core-owned tool policy (ADR-0011).

    Compiled to CLI flags by M1 adapters. Under yolo (ADR-0013) no policy is
    consulted at all and adapters receive ``yolo=True`` instead.
    """

    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    yolo: bool = False


@dataclass(frozen=True, slots=True)
class NodeDispatch:
    """Everything a runner needs to execute one attempt against a node."""

    node_id: str
    attempt: int
    prompt: str
    run_dir: Path
    model: str | None = None


@dataclass(frozen=True, slots=True)
class RunnerHandle:
    """Identity of a detached attempt process (ADR-0010).

    PIDs get reused: ``(pid, create_time)`` is the identity, never pid alone.
    """

    node_id: str
    attempt: int
    pid: int
    create_time: float
    run_dir: Path
    session_id: str | None = None
    workspace: Path | None = None
    """Set on resume templates so the adapter reuses the node's worktree."""


class AttemptStatus(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    TIMED_OUT = "timed_out"


class FailureClass(StrEnum):
    """Classification of a failed attempt (ADR-0011).

    Quota is distinct: it pauses dispatch instead of consuming retries.
    """

    NONE = "none"
    ERROR = "error"
    QUOTA = "quota"


@dataclass(frozen=True, slots=True)
class PollResult:
    status: AttemptStatus
    exit_code: int | None = None
    failure_class: FailureClass = FailureClass.NONE
    session_id: str | None = None
    cost: float | None = None
    """Normalized cost for the attempt so far; units are adapter-normalized
    at the boundary (ADR-0011). None when the runner cannot report cost."""


@dataclass(frozen=True, slots=True)
class RunnerCapabilities:
    supports_resume: bool = False
    reports_cost: bool = False


class RunnerPort(Protocol):
    """Vendor-neutral runner interface (spec §7.2)."""

    def start(self, node: NodeDispatch, workspace: Path, policy: ToolPolicy) -> RunnerHandle: ...

    def poll(self, handle: RunnerHandle) -> PollResult: ...

    def resume(self, handle: RunnerHandle, feedback: str) -> RunnerHandle:
        """Resume the original session with feedback; falls back to cold start."""
        ...

    def cancel(self, handle: RunnerHandle) -> None:
        """Kill a running attempt. Reached only via the runaway guard or a
        human cancel intent (ADR-0010)."""
        ...

    def capabilities(self) -> RunnerCapabilities: ...

"""Code host port (ADR-0002, spec §7.3).

The port exposes exactly what the merge ladder (ADR-0009) consumes: opaque
check conclusions, the review decision, and unresolved thread counts.
ticketflow never parses a diff and never judges code.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class CheckState(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class CheckConclusion:
    """One check's conclusion, consumed as an opaque signal (ADR-0009)."""

    name: str
    state: CheckState


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REVIEW_REQUIRED = "review_required"
    NONE = "none"
    """No review requirement configured and no review given."""


@dataclass(frozen=True, slots=True)
class PrStatus:
    """Everything a settle needs to walk the merge ladder (spec §9.1)."""

    number: int
    state: str  # open | merged | closed
    checks: tuple[CheckConclusion, ...]
    review_decision: ReviewDecision
    unresolved_threads: int
    mergeable: bool | None = None
    """False when the host reports merge conflicts; None while unknown."""

    @property
    def checks_pending(self) -> bool:
        return any(c.state is CheckState.PENDING for c in self.checks)

    @property
    def checks_failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.state is CheckState.FAILURE)

    @property
    def checks_green(self) -> bool:
        return not self.checks_pending and not self.checks_failed


@dataclass(frozen=True, slots=True)
class ReviewComment:
    """One normalized review comment (path, line, thread id, author)."""

    thread_id: str
    author: str
    body: str
    path: str | None = None
    line: int | None = None
    created_at: datetime | None = None


class CodeHostPort(Protocol):
    """Vendor-neutral code host interface (spec §7.3; ADR-0002 revision).

    ``repo_exists``/``default_branch`` serve workspace detection (ADR-0010),
    ``find_pr_for_branch``/``enable_auto_merge``/``rerun_failed_checks``
    serve the merge ladder and loop mechanics (ADR-0009).
    """

    def repo_exists(self) -> bool: ...

    def default_branch(self) -> str | None: ...

    def branch_exists(self, branch: str) -> bool: ...

    def open_pr(self, branch: str, title: str, body: str) -> int: ...

    def find_pr_for_branch(self, branch: str) -> int | None: ...

    def get_pr_status(self, pr_number: int) -> PrStatus: ...

    def get_feedback(self, pr_number: int, since: datetime | None) -> list[ReviewComment]: ...

    def resolve_thread(self, thread_id: str) -> None: ...

    def rerun_failed_checks(self, pr_number: int) -> bool:
        """Re-run failed checks once for flake handling (spec §9.2)."""
        ...

    def merge(self, pr_number: int) -> bool: ...

    def enable_auto_merge(self, pr_number: int) -> bool: ...

    def post_comment(self, pr_number: int, text: str) -> None: ...

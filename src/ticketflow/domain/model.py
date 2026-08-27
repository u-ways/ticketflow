"""Canonical domain types (ADR-0002). No vendor vocabulary crosses into here."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class NodeState(StrEnum):
    """Node lifecycle states (ADR-0006). Merged and Escalated are terminal."""

    BLOCKED = "blocked"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    AWAITING_SIGNALS = "awaiting_signals"
    ADDRESSING_FEEDBACK = "addressing_feedback"
    MERGED = "merged"
    ESCALATED = "escalated"


class IntentType(StrEnum):
    """Known human-signal types (ADR-0004).

    Intents are stored and dispatched by their string value; unknown types are
    accepted and recorded so future signal kinds (e.g. plan approval,
    ADR-0014) need no schema change.
    """

    APPROVE = "approve"
    REJECT = "reject"
    UNBLOCK = "unblock"
    CANCEL = "cancel"
    RETRY = "retry"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class Node:
    """Canonical unit of work, identified independently of any tracker."""

    node_id: str
    title: str
    body: str
    state: NodeState
    blocked_reason: str | None = None
    attempt_count: int = 0
    cycle_count: int = 0
    crash_count: int = 0
    scope_hints: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExternalRef:
    """Mapping of a tracker/code-host item onto a node."""

    node_id: str
    provider: str
    external_key: str
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class Attempt:
    """One dispatch of a runner against a node. Owns its run dir and trace."""

    node_id: str
    attempt: int
    runner: str
    run_dir: str
    status: str = "running"
    model: str | None = None
    pid: int | None = None
    create_time: float | None = None
    session_id: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Lease:
    """Exclusive claim on a node, preventing double dispatch (ADR-0008)."""

    node_id: str
    worker_id: str
    attempt: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Intent:
    """A normalized human signal (ADR-0004)."""

    intent_id: int
    intent_type: str
    source: str
    node_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    processed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """One append-only event-log row (ADR-0005)."""

    event_id: int
    ts: datetime
    kind: str
    node_id: str | None = None
    attempt: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

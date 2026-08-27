"""The plan lifecycle state machine and plan row types (ADR-0014).

Plan statuses live in ``plans.status`` and never in ``nodes.state`` — the
node state machine (ADR-0006) is untouched by planning. Like
:mod:`ticketflow.domain.transitions`, the table is explicit and the store's
``set_plan_status`` refuses any edge not present here. ``EMITTING`` has no
path to ``DISCARDED``: there is no rollback for a partially emitted plan —
re-running emit is the recovery path (ADR-0014).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from ticketflow.domain.errors import IllegalPlanTransition


class PlanStatus(StrEnum):
    """Plan lifecycle states (spec §13.3 plus the terminal ``emitted``)."""

    INGESTED = "ingested"
    GROUNDING = "grounding"
    SYNTHESIS = "synthesis"
    IN_REVIEW = "in_review"
    EMITTING = "emitting"
    EMITTED = "emitted"
    DISCARDED = "discarded"


PLAN_TRANSITIONS: MappingProxyType[tuple[PlanStatus, PlanStatus], str] = MappingProxyType(
    {
        (PlanStatus.INGESTED, PlanStatus.GROUNDING): "grounding attempt dispatched",
        (PlanStatus.GROUNDING, PlanStatus.SYNTHESIS): "research brief captured",
        (PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW): "first valid revision recorded",
        (PlanStatus.IN_REVIEW, PlanStatus.EMITTING): (
            "approval intent consumed; the approved revision is pinned (ADR-0004)"
        ),
        (PlanStatus.EMITTING, PlanStatus.EMITTED): (
            "every item and edge exists on the tracker — only then (ADR-0014)"
        ),
        (PlanStatus.INGESTED, PlanStatus.DISCARDED): "abandoned before grounding",
        (PlanStatus.GROUNDING, PlanStatus.DISCARDED): "abandoned during grounding",
        (PlanStatus.SYNTHESIS, PlanStatus.DISCARDED): "abandoned during synthesis",
        (PlanStatus.IN_REVIEW, PlanStatus.DISCARDED): "human rejected the plan",
        # EMITTING -> DISCARDED deliberately absent: no rollback (ADR-0014).
    }
)


def assert_legal_plan(from_status: PlanStatus, to_status: PlanStatus) -> None:
    """Raise :class:`IllegalPlanTransition` unless the edge is in the table."""
    if (from_status, to_status) not in PLAN_TRANSITIONS:
        raise IllegalPlanTransition(f"illegal plan transition: {from_status} -> {to_status}")


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """One row of the ``plans`` table."""

    plan_id: str
    provider: str
    epic_key: str
    status: PlanStatus
    current_revision: int = 0
    approved_revision: int | None = None
    grounding_attempts: int = 0
    grounding_pid: int | None = None
    grounding_create_time: float | None = None
    session_id: str | None = None
    brief: str | None = None
    discard_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """One immutable plan revision: the byte-exact YAML at that turn."""

    plan_id: str
    revision: int
    source: str
    """``synthesis`` | ``revision`` | ``human_edit``."""
    yaml: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EmittedPlanItem:
    """One row of the emission ledger; the PK is the idempotency key."""

    plan_id: str
    item_index: int
    external_key: str
    created_at: datetime | None = None
    edges_written_at: datetime | None = None
    mirrored_at: datetime | None = None

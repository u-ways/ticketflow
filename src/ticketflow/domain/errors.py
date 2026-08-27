"""Domain error hierarchy."""


class TicketflowError(Exception):
    """Base class for all ticketflow errors."""


class IllegalTransition(TicketflowError):
    """A node state change not present in the transition table (ADR-0006)."""


class UnknownNode(TicketflowError):
    """An operation referenced a node_id that does not exist."""


class DependencyCycle(TicketflowError):
    """The dependency graph contains a cycle and cannot be scheduled (ADR-0008)."""


class QuotaExhausted(TicketflowError):
    """A provider-side quota or rate limit was hit (ADR-0011).

    Distinct from ordinary failures: dispatch pauses instead of consuming
    retry attempts; a human resumes via an intent.
    """


class UnknownEpic(TicketflowError):
    """The tracker has no item with the given key, or the epic has no live
    plan (ADR-0014)."""


class PlanTurnRefused(TicketflowError):
    """A planner turn was invoked in a status that forbids it (ADR-0014) —
    e.g. editing a plan after approval, or emitting before one."""


class GroundingFailed(TicketflowError):
    """The grounding agent crashed, timed out, or produced no brief (ADR-0014).

    The plan stays in ``grounding``; re-running the turn is the recovery
    path, exactly like a failed emit."""


class PlanValidationError(TicketflowError):
    """A plan revision failed schema or semantic validation (ADR-0014).

    Raised per turn: the offending revision is rejected, the previous one
    stands (spec §13.5 — reject the turn, not the plan).
    """


class IllegalPlanTransition(TicketflowError):
    """A plan status change not present in the plan transition table (ADR-0014)."""

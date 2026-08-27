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

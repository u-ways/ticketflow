"""The node state machine as one explicit table (ADR-0006).

No code path mutates a node's state except through this module: the store's
``set_state`` calls :func:`assert_legal` before writing.
"""

from ticketflow.domain.errors import IllegalTransition
from ticketflow.domain.model import NodeState

# (from_state, to_state) pairs. Guards are enforced at the call sites named in
# the comments; the table is the single source of which edges exist at all.
TRANSITIONS: frozenset[tuple[NodeState, NodeState]] = frozenset(
    {
        # All upstream edges resolved — the only place graph structure matters.
        (NodeState.BLOCKED, NodeState.READY),
        # Lease claimed before dispatch (ADR-0008).
        (NodeState.READY, NodeState.IN_PROGRESS),
        # Runner exited, branch pushed, PR opened.
        (NodeState.IN_PROGRESS, NodeState.AWAITING_SIGNALS),
        # Bootstrap: no repo existed, so the initial push completes the node
        # (spec §9.1 step 0; ADR-0006 revision note).
        (NodeState.IN_PROGRESS, NodeState.MERGED),
        # Lease expired without a heartbeat.
        (NodeState.IN_PROGRESS, NodeState.READY),
        # Crash, timeout, empty diff, repeated lease expiry, policy denial,
        # quota exhaustion.
        (NodeState.IN_PROGRESS, NodeState.ESCALATED),
        # CI red or review comments arrived.
        (NodeState.AWAITING_SIGNALS, NodeState.ADDRESSING_FEEDBACK),
        # Agent pushed a fix.
        (NodeState.ADDRESSING_FEEDBACK, NodeState.AWAITING_SIGNALS),
        # Checks green + approvals satisfied + threads resolved.
        (NodeState.AWAITING_SIGNALS, NodeState.MERGED),
        # Checks stuck red past the cycle cap, or policy violation.
        (NodeState.AWAITING_SIGNALS, NodeState.ESCALATED),
        # Feedback cycle cap exceeded.
        (NodeState.ADDRESSING_FEEDBACK, NodeState.ESCALATED),
        # A human intent re-enters the machine (ADR-0004); the only exit.
        (NodeState.ESCALATED, NodeState.READY),
        # Operator cancelled/rejected a node before it ever ran
        # (ADR-0006 revision note).
        (NodeState.BLOCKED, NodeState.ESCALATED),
        (NodeState.READY, NodeState.ESCALATED),
    }
)


def assert_legal(from_state: NodeState, to_state: NodeState) -> None:
    """Raise :class:`IllegalTransition` unless the edge is in the table."""
    if (from_state, to_state) not in TRANSITIONS:
        raise IllegalTransition(f"illegal node transition: {from_state} -> {to_state}")

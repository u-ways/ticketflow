"""The node state machine as one explicit table (ADR-0006).

Each entry is ``(from_state, to_state) -> guard``. The guard names the
condition under which the edge may be taken; its predicate is enforced at the
single orchestrator call site that takes the edge, because evaluating it needs
I/O context (PR status, the lease table, run dirs) that the domain layer must
not import — see the ADR-0006 revision note. No code path mutates a node's
state except through this module: the store's ``set_state`` calls
:func:`assert_legal` before writing.
"""

from types import MappingProxyType

from ticketflow.domain.errors import IllegalTransition
from ticketflow.domain.model import NodeState

TRANSITIONS: MappingProxyType[tuple[NodeState, NodeState], str] = MappingProxyType(
    {
        (NodeState.BLOCKED, NodeState.READY): (
            "all upstream edges resolved — the only place graph structure matters"
        ),
        (NodeState.READY, NodeState.IN_PROGRESS): "lease claimed before dispatch (ADR-0008)",
        (NodeState.IN_PROGRESS, NodeState.AWAITING_SIGNALS): (
            "runner exited cleanly, branch pushed, PR opened"
        ),
        (NodeState.IN_PROGRESS, NodeState.MERGED): (
            "bootstrap: no repo existed and the initial push succeeded "
            "(spec §9.1 step 0; ADR-0006 revision)"
        ),
        (NodeState.IN_PROGRESS, NodeState.READY): "lease expired without a heartbeat",
        (NodeState.IN_PROGRESS, NodeState.ESCALATED): (
            "crash, timeout, empty diff, repeated lease expiry, policy denial, or quota exhaustion"
        ),
        (NodeState.AWAITING_SIGNALS, NodeState.ADDRESSING_FEEDBACK): (
            "CI red or review comments arrived after every check reported"
        ),
        (NodeState.ADDRESSING_FEEDBACK, NodeState.AWAITING_SIGNALS): "agent pushed a fix",
        (NodeState.AWAITING_SIGNALS, NodeState.MERGED): (
            "checks green AND approvals satisfied AND review threads resolved"
        ),
        (NodeState.AWAITING_SIGNALS, NodeState.ESCALATED): (
            "checks stuck red past the cycle cap, or policy violation"
        ),
        (NodeState.ADDRESSING_FEEDBACK, NodeState.ESCALATED): "feedback cycle cap exceeded",
        (NodeState.ESCALATED, NodeState.READY): (
            "a human intent re-enters the machine (ADR-0004); the only exit"
        ),
        (NodeState.BLOCKED, NodeState.ESCALATED): (
            "operator cancel/reject intent before the node ever ran (ADR-0006 revision)"
        ),
        (NodeState.READY, NodeState.ESCALATED): (
            "operator cancel/reject intent, or irrecoverable dispatch failure (ADR-0006 revision)"
        ),
    }
)


def assert_legal(from_state: NodeState, to_state: NodeState) -> None:
    """Raise :class:`IllegalTransition` unless the edge is in the table."""
    if (from_state, to_state) not in TRANSITIONS:
        raise IllegalTransition(f"illegal node transition: {from_state} -> {to_state}")

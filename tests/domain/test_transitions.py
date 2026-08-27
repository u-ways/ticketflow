"""The node state machine is an explicit, closed transition table (ADR-0006).

Every lifecycle path is enumerable; an illegal transition raises. No code path
may mutate a node's state except through this table.
"""

import pytest

from ticketflow.domain.errors import IllegalTransition
from ticketflow.domain.model import NodeState
from ticketflow.domain.transitions import TRANSITIONS, assert_legal


class TestTransitionTable:
    def test_table_is_exactly_the_adr_0006_set(self) -> None:
        expected = {
            # Blocked → Ready: all upstream edges resolved.
            (NodeState.BLOCKED, NodeState.READY),
            # Ready → InProgress: lease claimed before dispatch.
            (NodeState.READY, NodeState.IN_PROGRESS),
            # InProgress → AwaitingSignals: runner exited, branch pushed, PR opened.
            (NodeState.IN_PROGRESS, NodeState.AWAITING_SIGNALS),
            # InProgress → Merged: bootstrap — no repo existed, the initial push
            # completes the node (spec §9.1 step 0; ADR-0006 revision).
            (NodeState.IN_PROGRESS, NodeState.MERGED),
            # InProgress → Ready: lease expired without a heartbeat.
            (NodeState.IN_PROGRESS, NodeState.READY),
            # InProgress → Escalated: crash, timeout, empty diff, quota, policy.
            (NodeState.IN_PROGRESS, NodeState.ESCALATED),
            # AwaitingSignals → AddressingFeedback: CI red or review comments.
            (NodeState.AWAITING_SIGNALS, NodeState.ADDRESSING_FEEDBACK),
            # AddressingFeedback → AwaitingSignals: agent pushed.
            (NodeState.ADDRESSING_FEEDBACK, NodeState.AWAITING_SIGNALS),
            # AwaitingSignals → Merged: green + approved + threads resolved.
            (NodeState.AWAITING_SIGNALS, NodeState.MERGED),
            # AwaitingSignals → Escalated: checks stuck red past the cycle cap.
            (NodeState.AWAITING_SIGNALS, NodeState.ESCALATED),
            # AddressingFeedback → Escalated: feedback cycle cap exceeded.
            (NodeState.ADDRESSING_FEEDBACK, NodeState.ESCALATED),
            # Escalated → Ready: a human intent re-enters the machine.
            (NodeState.ESCALATED, NodeState.READY),
            # Blocked → Escalated: cancelled or rejected before ever running.
            (NodeState.BLOCKED, NodeState.ESCALATED),
            # Ready → Escalated: cancelled, or dispatch failed irrecoverably.
            (NodeState.READY, NodeState.ESCALATED),
        }
        assert expected == set(TRANSITIONS)

    def test_every_entry_names_its_guard(self) -> None:
        # ADR-0006: the table is (from, to, guard); the guard is a named
        # condition enforced at the edge's single orchestrator call site.
        for pair, guard in TRANSITIONS.items():
            assert isinstance(guard, str) and guard, f"missing guard for {pair}"

    @pytest.mark.parametrize("pair", sorted(TRANSITIONS))
    def test_every_tabled_transition_is_legal(self, pair: tuple[NodeState, NodeState]) -> None:
        assert_legal(*pair)  # must not raise

    @pytest.mark.parametrize(
        "pair",
        [
            (NodeState.BLOCKED, NodeState.IN_PROGRESS),  # must go through Ready
            (NodeState.READY, NodeState.MERGED),  # cannot merge without running
            (NodeState.MERGED, NodeState.READY),  # merged is terminal
            (NodeState.MERGED, NodeState.ESCALATED),  # merged is terminal
            (NodeState.ESCALATED, NodeState.IN_PROGRESS),  # re-entry is at Ready
            (NodeState.ESCALATED, NodeState.MERGED),  # no silent resolution
            (NodeState.AWAITING_SIGNALS, NodeState.READY),  # loop, don't restart
            (NodeState.AWAITING_SIGNALS, NodeState.IN_PROGRESS),
            (NodeState.ADDRESSING_FEEDBACK, NodeState.MERGED),  # must settle first
            (NodeState.BLOCKED, NodeState.MERGED),
        ],
    )
    def test_illegal_transitions_raise(self, pair: tuple[NodeState, NodeState]) -> None:
        with pytest.raises(IllegalTransition):
            assert_legal(*pair)

    def test_self_transition_is_illegal(self) -> None:
        for state in NodeState:
            with pytest.raises(IllegalTransition):
                assert_legal(state, state)

    def test_terminal_states_have_no_orchestrator_exit(self) -> None:
        # Merged is fully terminal. Escalated exits only to Ready, and only a
        # human intent takes that edge (enforced at the call site, ADR-0004).
        outgoing_from_merged = {t for t in TRANSITIONS if t[0] is NodeState.MERGED}
        outgoing_from_escalated = {t for t in TRANSITIONS if t[0] is NodeState.ESCALATED}
        assert outgoing_from_merged == set()
        assert outgoing_from_escalated == {(NodeState.ESCALATED, NodeState.READY)}

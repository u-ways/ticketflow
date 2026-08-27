"""Deterministic ready-set computation on graphlib (ADR-0008).

Cycles are rejected at load. Scope hints bias dispatch order but never block.
Dependents of an escalated node surface their root cause (ADR-0006).
"""

import pytest

from ticketflow.domain.errors import DependencyCycle
from ticketflow.domain.model import NodeState
from ticketflow.graph.ready import (
    blocked_on_escalated,
    detect_cycles,
    newly_ready,
    stagger_order,
)

B = NodeState.BLOCKED
R = NodeState.READY
M = NodeState.MERGED
E = NodeState.ESCALATED


class TestCycleDetection:
    def test_acyclic_graph_passes(self) -> None:
        detect_cycles({("a", "b"), ("b", "c"), ("a", "c")})  # must not raise

    def test_cycle_raises_with_members(self) -> None:
        with pytest.raises(DependencyCycle) as exc:
            detect_cycles({("a", "b"), ("b", "c"), ("c", "a")})
        for member in ("a", "b", "c"):
            assert member in str(exc.value)

    def test_self_dependency_is_a_cycle(self) -> None:
        with pytest.raises(DependencyCycle):
            detect_cycles({("a", "a")})


class TestNewlyReady:
    def test_root_nodes_are_ready_immediately(self) -> None:
        assert newly_ready({"a": B, "b": B}, edges=set()) == ("a", "b")

    def test_blocked_until_all_upstreams_merged(self) -> None:
        nodes = {"a": M, "b": B, "c": B}
        # c depends on both a (merged) and b (not merged).
        edges = {("a", "c"), ("b", "c")}
        assert newly_ready(nodes, edges) == ("b",)

    def test_diamond_resolves_when_both_arms_merge(self) -> None:
        edges = {("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")}
        assert newly_ready({"a": M, "b": M, "c": M, "d": B}, edges) == ("d",)
        assert newly_ready({"a": M, "b": M, "c": R, "d": B}, edges) == ()

    def test_only_blocked_nodes_are_candidates(self) -> None:
        # Already-ready or in-flight nodes must not reappear.
        nodes = {"a": M, "b": R, "c": NodeState.IN_PROGRESS}
        assert newly_ready(nodes, edges={("a", "b"), ("a", "c")}) == ()

    def test_escalated_upstream_does_not_unblock(self) -> None:
        assert newly_ready({"a": E, "b": B}, edges={("a", "b")}) == ()

    def test_deterministic_order(self) -> None:
        nodes = {"z": B, "a": B, "m": B}
        assert newly_ready(nodes, edges=set()) == ("a", "m", "z")


class TestBlockedOnEscalated:
    def test_direct_and_transitive_dependents_name_the_root(self) -> None:
        edges = {("a", "b"), ("b", "c")}
        reasons = blocked_on_escalated({"a": E, "b": B, "c": B}, edges)
        assert reasons == {"b": "a", "c": "a"}

    def test_no_escalations_means_no_reasons(self) -> None:
        assert blocked_on_escalated({"a": M, "b": B}, {("a", "b")}) == {}

    def test_only_blocked_dependents_are_annotated(self) -> None:
        # b already merged before a escalated on retry: nothing to annotate.
        assert blocked_on_escalated({"a": E, "b": M}, {("a", "b")}) == {}


class TestStaggerOrder:
    def test_non_overlapping_nodes_come_first(self) -> None:
        ready = [
            ("x", ("src/api/",)),
            ("y", ("docs/",)),
            ("z", ("src/",)),
        ]
        ordered = stagger_order(ready, in_flight_scopes=[("src/",)])
        assert [n for n, _ in ordered] == ["y", "x", "z"]

    def test_never_drops_a_node(self) -> None:
        ready = [("x", ("src/",)), ("y", ("src/",))]
        ordered = stagger_order(ready, in_flight_scopes=[("src/",)])
        assert {n for n, _ in ordered} == {"x", "y"}

    def test_no_hints_means_no_bias(self) -> None:
        ready = [("x", ()), ("y", ())]
        ordered = stagger_order(ready, in_flight_scopes=[("src/",)])
        assert [n for n, _ in ordered] == ["x", "y"]

    def test_prefix_overlap_detected_both_directions(self) -> None:
        ready = [("deep", ("src/api/v1/",)), ("free", ("tests/",))]
        ordered = stagger_order(ready, in_flight_scopes=[("src/",)])
        assert [n for n, _ in ordered] == ["free", "deep"]

    def test_glob_suffixes_are_normalized(self) -> None:
        ready = [("x", ("src/**",)), ("y", ("docs/*",))]
        ordered = stagger_order(ready, in_flight_scopes=[("src/api.py",)])
        assert [n for n, _ in ordered] == ["y", "x"]

    def test_empty_in_flight_keeps_input_order(self) -> None:
        ready = [("b", ("src/",)), ("a", ("docs/",))]
        assert [n for n, _ in stagger_order(ready, in_flight_scopes=[])] == ["b", "a"]

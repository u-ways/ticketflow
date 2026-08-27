"""The plan schema carries per-edge confidence and cited evidence, and its
validators reject edges referencing nonexistent items; graphlib rejects
cycles (ADR-0014 review guidance, verbatim)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ticketflow.planner.schema import Plan, PlanEdge, PlanItem, derive_plan_id
from ticketflow.planner.validate import semantic_errors

PLAN_ID = "a3f8c2d91b04"
T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def make_plan(
    *,
    items: int = 3,
    edges: tuple[PlanEdge, ...] = (),
    unevidenced: tuple[PlanEdge, ...] = (),
) -> Plan:
    return Plan(
        plan_id=PLAN_ID,
        epic_key="#42",
        items=tuple(
            PlanItem(index=i, title=f"Item {i}", body=f"Do part {i}.") for i in range(items)
        ),
        edges=edges,
        unevidenced_edges=unevidenced,
    )


def edge(up: int, down: int, *, confidence: float = 0.9, evidence: str = "cited") -> PlanEdge:
    return PlanEdge(upstream=up, downstream=down, confidence=confidence, evidence=evidence)


class TestDerivePlanId:
    def test_twelve_hex_chars(self) -> None:
        plan_id = derive_plan_id("github", "#42", T0)
        assert len(plan_id) == 12
        assert int(plan_id, 16) >= 0

    def test_deterministic_for_same_inputs(self) -> None:
        assert derive_plan_id("github", "#42", T0) == derive_plan_id("github", "#42", T0)

    def test_replan_gets_a_distinct_id(self) -> None:
        later = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
        assert derive_plan_id("github", "#42", T0) != derive_plan_id("github", "#42", later)


class TestStructuralValidation:
    def test_valid_plan_accepted(self) -> None:
        plan = make_plan(edges=(edge(0, 1), edge(1, 2)))
        assert len(plan.items) == 3
        assert semantic_errors(plan) == ()

    def test_edge_to_nonexistent_item_rejected(self) -> None:
        with pytest.raises(ValidationError, match="nonexistent item"):
            make_plan(items=2, edges=(edge(0, 7),))

    def test_self_edge_rejected(self) -> None:
        with pytest.raises(ValidationError, match="self-edge"):
            make_plan(edges=(edge(1, 1),))

    def test_duplicate_edge_rejected_across_both_lists(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            make_plan(edges=(edge(0, 1),), unevidenced=(edge(0, 1, evidence=""),))

    def test_non_contiguous_indices_rejected(self) -> None:
        with pytest.raises(ValidationError, match="contiguous"):
            Plan(
                plan_id=PLAN_ID,
                epic_key="#42",
                items=(
                    PlanItem(index=0, title="a", body=""),
                    PlanItem(index=2, title="b", body=""),
                ),
            )

    def test_confidence_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            edge(0, 1, confidence=1.5)

    def test_evidenced_edge_without_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unevidenced_edges"):
            make_plan(edges=(edge(0, 1, evidence="  "),))

    def test_unevidenced_edges_may_lack_evidence(self) -> None:
        plan = make_plan(unevidenced=(edge(0, 1, confidence=0.4, evidence=""),))
        assert plan.unevidenced_edges[0].evidence == ""

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Plan(plan_id=PLAN_ID, epic_key="#42", items=())

    def test_malformed_plan_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="plan id"):
            Plan(
                plan_id="nope",
                epic_key="#42",
                items=(PlanItem(index=0, title="a", body=""),),
            )


class TestSemanticValidation:
    def test_cycle_rejected_with_path(self) -> None:
        plan = make_plan(edges=(edge(0, 1), edge(1, 2), edge(2, 0)))
        errors = semantic_errors(plan)
        assert len(errors) == 1
        assert "cycle" in errors[0]
        assert " -> " in errors[0]

    def test_cycle_through_unevidenced_edges_detected(self) -> None:
        plan = make_plan(
            edges=(edge(0, 1),),
            unevidenced=(edge(1, 0, confidence=0.2, evidence=""),),
        )
        assert any("cycle" in error for error in semantic_errors(plan))

    def test_item_body_with_grammar_lines_rejected(self) -> None:
        plan = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(PlanItem(index=0, title="a", body="Work.\n\ndepends-on: #7"),),
        )
        errors = semantic_errors(plan)
        assert len(errors) == 1
        assert "grammar lines" in errors[0]

    def test_malformed_scope_path_rejected_before_approval(self) -> None:
        # Everything the emit-time renderer would refuse must be refused at
        # revision time, or an approved plan could get stuck failing
        # emission forever (there is no rollback out of emitting).
        plan = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(PlanItem(index=0, title="a", body="", scope=("src/a.py, src/b.py",)),),
        )
        errors = semantic_errors(plan)
        assert len(errors) == 1
        assert "scope path" in errors[0]

    def test_clean_plan_has_no_errors(self) -> None:
        assert semantic_errors(make_plan(edges=(edge(0, 1),))) == ()

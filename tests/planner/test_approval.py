"""Approval consumption (ADR-0004, ADR-0014): idempotent, digest-pinned,
stale-refusing, and the source of the first-proposal-vs-approved diff."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.orchestrator.conftest import FakeClock
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.planner.approval import (
    consume_plan_intents,
    first_vs_approved_diff,
    yaml_sha256,
)
from ticketflow.planner.schema import Plan, PlanEdge, PlanItem
from ticketflow.planner.yaml_io import dump_plan
from ticketflow.store.store import Store

from .conftest import PLAN_ID


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "approval.db")
    yield s
    s.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def plan_with_edges(*edges: tuple[int, int, float]) -> Plan:
    return Plan(
        plan_id=PLAN_ID,
        epic_key="#42",
        items=(
            PlanItem(index=0, title="A", body="a"),
            PlanItem(index=1, title="B", body="b"),
            PlanItem(index=2, title="C", body="c"),
        ),
        edges=tuple(
            PlanEdge(upstream=up, downstream=down, confidence=conf, evidence="cited")
            for up, down, conf in edges
        ),
    )


def in_review(store: Store, clock: FakeClock, *revisions: Plan) -> PlanRecord:
    store.create_plan(plan_id=PLAN_ID, provider="github", epic_key="#42", now=clock())
    for status in (PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW):
        store.set_plan_status(PLAN_ID, status, now=clock())
    for revision in revisions:
        store.add_plan_revision(
            PLAN_ID, yaml_text=dump_plan(revision), source="synthesis", now=clock()
        )
    plan = store.get_plan(PLAN_ID)
    assert plan is not None
    return plan


def approval_intent(
    store: Store, clock: FakeClock, *, revision: int, sha: str | None = None
) -> None:
    blob = store.get_plan_revision(PLAN_ID, revision)
    store.add_intent(
        intent_type="plan_approve",
        source="cli",
        node_id=None,
        payload={
            "plan_id": PLAN_ID,
            "revision": revision,
            "yaml_sha256": sha if sha is not None else yaml_sha256(blob.yaml if blob else ""),
        },
        external_id=f"plan_approve:{PLAN_ID}:{revision}",
        now=clock(),
    )


class TestApprove:
    def test_pins_revision_and_records_the_diff(self, store: Store, clock: FakeClock) -> None:
        first = plan_with_edges((0, 1, 0.9), (1, 2, 0.35))
        approved = plan_with_edges((0, 1, 0.9))  # the reviewer pruned 1 -> 2
        plan = in_review(store, clock, first, approved)
        approval_intent(store, clock, revision=2)

        plan = consume_plan_intents(store, plan, clock=clock)

        assert plan.status is PlanStatus.EMITTING
        assert plan.approved_revision == 2
        [event] = [e for e in store.events_after(0) if e.kind == "plan_approved"]
        assert event.payload["edges_removed"] == [
            {"upstream": 1, "downstream": 2, "confidence": 0.35}
        ]
        assert event.payload["edges_added"] == []
        assert store.unprocessed_intents() == []

    def test_stale_revision_refused(self, store: Store, clock: FakeClock) -> None:
        plan = in_review(store, clock, plan_with_edges((0, 1, 0.9)), plan_with_edges())
        approval_intent(store, clock, revision=1)  # current is 2

        plan = consume_plan_intents(store, plan, clock=clock)

        assert plan.status is PlanStatus.IN_REVIEW
        kinds = [e.kind for e in store.events_after(0)]
        assert "plan_approval_stale" in kinds
        assert store.unprocessed_intents() == []  # processed either way (ADR-0004)

    def test_digest_mismatch_refused(self, store: Store, clock: FakeClock) -> None:
        plan = in_review(store, clock, plan_with_edges((0, 1, 0.9)))
        approval_intent(store, clock, revision=1, sha="0" * 64)
        plan = consume_plan_intents(store, plan, clock=clock)
        assert plan.status is PlanStatus.IN_REVIEW
        assert any(e.kind == "plan_approval_stale" for e in store.events_after(0))

    def test_reconsuming_is_a_noop(self, store: Store, clock: FakeClock) -> None:
        plan = in_review(store, clock, plan_with_edges((0, 1, 0.9)))
        approval_intent(store, clock, revision=1)
        plan = consume_plan_intents(store, plan, clock=clock)
        events_before = len(store.events_after(0))
        plan = consume_plan_intents(store, plan, clock=clock)
        assert plan.status is PlanStatus.EMITTING
        assert len(store.events_after(0)) == events_before

    def test_foreign_plan_intents_left_pending(self, store: Store, clock: FakeClock) -> None:
        plan = in_review(store, clock, plan_with_edges((0, 1, 0.9)))
        store.add_intent(
            intent_type="plan_approve",
            source="cli",
            node_id=None,
            payload={"plan_id": "f" * 12, "revision": 1, "yaml_sha256": "x"},
            external_id="plan_approve:other",
            now=clock(),
        )
        consume_plan_intents(store, plan, clock=clock)
        [pending] = store.unprocessed_intents()
        assert pending.payload["plan_id"] == "f" * 12


class TestReject:
    def test_rejection_discards_with_reason(self, store: Store, clock: FakeClock) -> None:
        plan = in_review(store, clock, plan_with_edges((0, 1, 0.9)))
        store.add_intent(
            intent_type="plan_reject",
            source="cli",
            node_id=None,
            payload={"plan_id": PLAN_ID, "reason": "wrong split"},
            external_id=f"plan_reject:{PLAN_ID}",
            now=clock(),
        )
        plan = consume_plan_intents(store, plan, clock=clock)
        assert plan.status is PlanStatus.DISCARDED
        assert plan.discard_reason == "wrong split"

    def test_rejection_after_approval_is_ignored(self, store: Store, clock: FakeClock) -> None:
        # No rollback: emitting cannot be discarded (ADR-0014).
        plan = in_review(store, clock, plan_with_edges((0, 1, 0.9)))
        approval_intent(store, clock, revision=1)
        plan = consume_plan_intents(store, plan, clock=clock)
        store.add_intent(
            intent_type="plan_reject",
            source="cli",
            node_id=None,
            payload={"plan_id": PLAN_ID, "reason": "too late"},
            external_id=f"plan_reject:{PLAN_ID}",
            now=clock(),
        )
        plan = consume_plan_intents(store, plan, clock=clock)
        assert plan.status is PlanStatus.EMITTING
        assert any(e.kind == "plan_intent_ignored" for e in store.events_after(0))


class TestDiff:
    def test_item_changes_tracked(self) -> None:
        first = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(PlanItem(index=0, title="A", body=""), PlanItem(index=1, title="B", body="")),
        )
        approved = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(
                PlanItem(index=0, title="A renamed", body=""),
                PlanItem(index=1, title="B", body=""),
                PlanItem(index=2, title="C", body=""),
            ),
        )
        diff = first_vs_approved_diff(first, approved)
        assert diff["items_added"] == [2]
        assert diff["items_removed"] == []
        assert diff["items_retitled"] == [0]

    def test_unevidenced_edges_participate(self) -> None:
        first = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(PlanItem(index=0, title="A", body=""), PlanItem(index=1, title="B", body="")),
            unevidenced_edges=(PlanEdge(upstream=0, downstream=1, confidence=0.2),),
        )
        approved = Plan(plan_id=PLAN_ID, epic_key="#42", items=first.items)
        diff = first_vs_approved_diff(first, approved)
        assert diff["edges_removed"] == [{"upstream": 0, "downstream": 1, "confidence": 0.2}]

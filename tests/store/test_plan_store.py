"""Plan tables (migration 4, ADR-0014): lifecycle rows, append-only
revisions, and the emission ledger whose primary key is the idempotency key.
"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ticketflow.domain.errors import IllegalPlanTransition, UnknownPlan
from ticketflow.domain.plan import PlanStatus
from ticketflow.store.store import Store

T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
PLAN_ID = "a3f8c2d91b04"


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "ticketflow.db")
    yield s
    s.close()


def make_plan(store: Store, plan_id: str = PLAN_ID, epic_key: str = "#42") -> str:
    store.create_plan(plan_id=plan_id, provider="github", epic_key=epic_key, now=T0)
    return plan_id


def advance(store: Store, plan_id: str, *statuses: PlanStatus) -> None:
    for i, status in enumerate(statuses):
        store.set_plan_status(plan_id, status, now=at(i + 1))


class TestMigration4:
    def test_schema_version_is_4(self, store: Store) -> None:
        assert store.schema_version() == 4

    def test_reopen_is_a_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "tf.db"
        Store.open(path).close()
        reopened = Store.open(path)
        assert reopened.schema_version() == 4
        reopened.close()


class TestPlans:
    def test_create_and_get_roundtrip(self, store: Store) -> None:
        make_plan(store)
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.status is PlanStatus.INGESTED
        assert plan.provider == "github"
        assert plan.epic_key == "#42"
        assert plan.current_revision == 0
        assert [e.kind for e in store.events_after(0)] == ["plan_created"]

    def test_get_missing_returns_none(self, store: Store) -> None:
        assert store.get_plan("ghost") is None
        assert store.plan_status("ghost") is None

    def test_one_live_plan_per_epic(self, store: Store) -> None:
        make_plan(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.create_plan(plan_id="b" * 12, provider="github", epic_key="#42", now=at(1))

    def test_replan_allowed_after_discard(self, store: Store) -> None:
        make_plan(store)
        store.set_plan_status(PLAN_ID, PlanStatus.DISCARDED, now=at(1), reason="rejected")
        store.create_plan(plan_id="b" * 12, provider="github", epic_key="#42", now=at(2))
        active = store.plan_for_epic("github", "#42")
        assert active is not None
        assert active.plan_id == "b" * 12

    def test_plan_for_epic_ignores_terminal_plans(self, store: Store) -> None:
        make_plan(store)
        store.set_plan_status(PLAN_ID, PlanStatus.DISCARDED, now=at(1), reason="no")
        assert store.plan_for_epic("github", "#42") is None

    def test_list_plans_filters_by_status(self, store: Store) -> None:
        make_plan(store)
        make_plan(store, plan_id="b" * 12, epic_key="#43")
        store.set_plan_status("b" * 12, PlanStatus.GROUNDING, now=at(1))
        assert [p.plan_id for p in store.list_plans(PlanStatus.GROUNDING)] == ["b" * 12]
        assert len(store.list_plans()) == 2


class TestPlanTransitions:
    def test_legal_transition_applies_and_events(self, store: Store) -> None:
        make_plan(store)
        plan = store.set_plan_status(PLAN_ID, PlanStatus.GROUNDING, now=at(1))
        assert plan.status is PlanStatus.GROUNDING
        kinds = [e.kind for e in store.events_after(0)]
        assert "plan_status_changed" in kinds

    def test_illegal_transition_raises_and_changes_nothing(self, store: Store) -> None:
        make_plan(store)
        with pytest.raises(IllegalPlanTransition):
            store.set_plan_status(PLAN_ID, PlanStatus.EMITTED, now=at(1))
        assert store.plan_status(PLAN_ID) is PlanStatus.INGESTED

    def test_emitting_discard_is_the_guarded_retraction_edge(self, store: Store) -> None:
        # The edge exists for retracting an approval nothing has acted on;
        # its empty-ledger guard is enforced at the call site (ADR-0014).
        make_plan(store)
        advance(store, PLAN_ID, PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW)
        store.approve_plan(PLAN_ID, 1, now=at(4), diff={})
        plan = store.set_plan_status(PLAN_ID, PlanStatus.DISCARDED, now=at(5), reason="retracted")
        assert plan.status is PlanStatus.DISCARDED

    def test_discard_records_reason(self, store: Store) -> None:
        make_plan(store)
        plan = store.set_plan_status(PLAN_ID, PlanStatus.DISCARDED, now=at(1), reason="rejected")
        assert plan.discard_reason == "rejected"

    def test_unknown_plan_raises(self, store: Store) -> None:
        with pytest.raises(UnknownPlan):
            store.set_plan_status("ghost", PlanStatus.GROUNDING, now=T0)


class TestGroundingBookkeeping:
    def test_attempt_is_claimed_separately_from_the_process(self, store: Store) -> None:
        # The bump happens BEFORE dispatch (monotonic, ADR-0008); the pid
        # lands after — a crash in between must still consume the number.
        make_plan(store)
        assert store.bump_grounding_attempt(PLAN_ID, now=at(1)) == 1
        assert store.bump_grounding_attempt(PLAN_ID, now=at(2)) == 2
        store.set_grounding_process(PLAN_ID, pid=456, create_time=2.5, now=at(3))
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.grounding_attempts == 2
        assert plan.grounding_pid == 456
        assert plan.grounding_create_time == 2.5

    def test_session_and_brief_stored(self, store: Store) -> None:
        make_plan(store)
        store.set_plan_session(PLAN_ID, "sess-9", now=at(1))
        store.set_plan_brief(PLAN_ID, "# Brief\nFindings.", now=at(2))
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.session_id == "sess-9"
        assert plan.brief == "# Brief\nFindings."


class TestRevisions:
    def test_revision_numbering_is_monotonic(self, store: Store) -> None:
        make_plan(store)
        assert store.add_plan_revision(PLAN_ID, yaml_text="a: 1", source="synthesis", now=T0) == 1
        assert store.add_plan_revision(PLAN_ID, yaml_text="a: 2", source="revision", now=at(1)) == 2
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.current_revision == 2

    def test_revisions_are_byte_exact_and_immutable(self, store: Store) -> None:
        make_plan(store)
        store.add_plan_revision(PLAN_ID, yaml_text="raw: yaml\n", source="human_edit", now=T0)
        revision = store.get_plan_revision(PLAN_ID, 1)
        assert revision is not None
        assert revision.yaml == "raw: yaml\n"
        assert revision.source == "human_edit"
        assert store.get_plan_revision(PLAN_ID, 99) is None


class TestApproval:
    def test_approve_pins_revision_and_records_diff(self, store: Store) -> None:
        make_plan(store)
        advance(store, PLAN_ID, PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW)
        store.add_plan_revision(PLAN_ID, yaml_text="a: 1", source="synthesis", now=at(4))
        plan = store.approve_plan(PLAN_ID, 1, now=at(5), diff={"edges_removed": 1})
        assert plan.status is PlanStatus.EMITTING
        assert plan.approved_revision == 1
        approved = [e for e in store.events_after(0) if e.kind == "plan_approved"]
        assert len(approved) == 1
        assert approved[0].payload["edges_removed"] == 1

    def test_approve_outside_review_raises(self, store: Store) -> None:
        make_plan(store)
        with pytest.raises(IllegalPlanTransition):
            store.approve_plan(PLAN_ID, 1, now=at(1), diff={})

    def test_approval_rolls_back_with_its_event(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR-0005: the status flip and the plan_approved event are one unit
        # of work — if the event append fails, the approval never happened.
        make_plan(store)
        advance(store, PLAN_ID, PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW)
        store.add_plan_revision(PLAN_ID, yaml_text="a: 1", source="synthesis", now=at(4))

        def boom(**_kwargs: object) -> int:
            raise RuntimeError("event append failed")

        monkeypatch.setattr(store, "_append_event_row", boom)
        with pytest.raises(RuntimeError):
            store.approve_plan(PLAN_ID, 1, now=at(5), diff={})
        plan = store.get_plan(PLAN_ID)
        assert plan is not None
        assert plan.status is PlanStatus.IN_REVIEW
        assert plan.approved_revision is None


class TestEmissionLedger:
    def emitting_plan(self, store: Store) -> str:
        make_plan(store)
        advance(store, PLAN_ID, PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW)
        store.approve_plan(PLAN_ID, 1, now=at(4), diff={})
        return PLAN_ID

    def test_record_is_idempotent(self, store: Store) -> None:
        plan_id = self.emitting_plan(store)
        assert store.record_emitted_item(plan_id, 0, external_key="#101", now=at(5)) is True
        assert store.record_emitted_item(plan_id, 0, external_key="#999", now=at(6)) is False
        items = store.emitted_items(plan_id)
        assert len(items) == 1
        assert items[0].external_key == "#101"  # the retry adopted, not overwrote

    def test_memos_are_write_once(self, store: Store) -> None:
        plan_id = self.emitting_plan(store)
        store.record_emitted_item(plan_id, 0, external_key="#101", now=at(5))
        assert store.mark_item_edges_written(plan_id, 0, now=at(6)) is True
        assert store.mark_item_edges_written(plan_id, 0, now=at(7)) is False
        assert store.mark_item_mirrored(plan_id, 0, now=at(8)) is True
        assert store.mark_item_mirrored(plan_id, 0, now=at(9)) is False
        item = store.emitted_items(plan_id)[0]
        assert item.edges_written_at == at(6)
        assert item.mirrored_at == at(8)

    def test_items_ordered_by_index(self, store: Store) -> None:
        plan_id = self.emitting_plan(store)
        store.record_emitted_item(plan_id, 2, external_key="#103", now=at(5))
        store.record_emitted_item(plan_id, 0, external_key="#101", now=at(6))
        assert [i.item_index for i in store.emitted_items(plan_id)] == [0, 2]

"""Emission (ADR-0014): items before edges, per-item idempotency keys,
adopt-on-retry, best-effort mirrors, and no rollback path — the
crash-recovery walkthroughs as tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakeTracker
from tests.orchestrator.conftest import FakeClock
from ticketflow.domain.parser import parse_body, render_child_body
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.planner.emit import plan_label, run_emit
from ticketflow.planner.schema import Plan, PlanEdge, PlanItem
from ticketflow.planner.yaml_io import dump_plan
from ticketflow.ports.tracker import TrackerItem
from ticketflow.store.store import Store

from .conftest import PLAN_ID


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "emit.db")
    yield s
    s.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker() -> FakeTracker:
    tracker = FakeTracker()
    tracker.items.append(
        TrackerItem(provider="github", external_key="#42", title="Epic", body="Make it so.")
    )
    return tracker


def three_item_plan(*, unevidenced: bool = False) -> Plan:
    """0 -> 1 -> 2; the second edge optionally unevidenced."""
    second = PlanEdge(
        upstream=1, downstream=2, confidence=0.4, evidence="" if unevidenced else "cited"
    )
    return Plan(
        plan_id=PLAN_ID,
        epic_key="#42",
        items=(
            PlanItem(index=0, title="Scaffold", body="Create it.", scope=("src/",)),
            PlanItem(index=1, title="Feature", body="Build it."),
            PlanItem(index=2, title="CLI", body="Wrap it."),
        ),
        edges=(PlanEdge(upstream=0, downstream=1, confidence=0.9, evidence="cited"),)
        + (() if unevidenced else (second,)),
        unevidenced_edges=(second,) if unevidenced else (),
    )


def approved(store: Store, clock: FakeClock, plan: Plan) -> PlanRecord:
    store.create_plan(plan_id=PLAN_ID, provider="github", epic_key="#42", now=clock())
    for status in (PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW):
        store.set_plan_status(PLAN_ID, status, now=clock())
    store.add_plan_revision(PLAN_ID, yaml_text=dump_plan(plan), source="synthesis", now=clock())
    record = store.approve_plan(PLAN_ID, 1, now=clock(), diff={})
    return record


class TestHappyPath:
    def test_emits_items_then_edges_then_mirrors(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        record = approved(store, clock, three_item_plan())
        report = run_emit(store, tracker, record, clock=clock)

        assert report.complete
        assert report.created == 3
        assert report.edges_written == 3  # every body normalized, roots too
        assert report.mirrored == 2
        assert store.plan_status(PLAN_ID) is PlanStatus.EMITTED

        # Phase 1: marker + label + epic as parent, no depends-on yet.
        _key0, body0, labels0, parent0 = tracker.created[0]
        assert labels0 == (plan_label(PLAN_ID),)
        assert parent0 == "#42"
        assert parse_body(body0).plan_marker == (PLAN_ID, 0)
        assert parse_body(body0).depends_on == ()
        assert parse_body(body0).scope == ("src/",)

        # Phase 2: every body rewritten from the approved revision (the
        # root included, without a depends-on line), round-trippable.
        keys = [key for key, _, _, _ in tracker.created]
        (update0_key, update0_body), (update1_key, update1_body), (update2_key, update2_body) = (
            tracker.body_updates
        )
        assert update0_key == keys[0]
        assert parse_body(update0_body).depends_on == ()
        assert update1_key == keys[1]
        assert parse_body(update1_body).depends_on == (keys[0],)
        assert update2_key == keys[2]
        assert parse_body(update2_body).depends_on == (keys[1],)

        # Phase 3: native mirrors, one call per dependent item.
        assert tracker.mirrored == [(keys[1], (keys[0],)), (keys[2], (keys[1],))]

        # The epic hears about its children.
        epic_comments = [text for key, text in tracker.comments if key == "#42"]
        assert len(epic_comments) == 1
        assert all(key in epic_comments[0] for key in keys)

    def test_unevidenced_edges_are_emitted_too(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        # Keeping a proposed edge serializes (safe); dropping it would
        # parallelise unsafely (spec §13.2).
        record = approved(store, clock, three_item_plan(unevidenced=True))
        report = run_emit(store, tracker, record, clock=clock)
        assert report.complete
        keys = [key for key, _, _, _ in tracker.created]
        assert parse_body(tracker.body_updates[2][1]).depends_on == (keys[1],)

    def test_unapproved_plan_refused(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        store.create_plan(plan_id=PLAN_ID, provider="github", epic_key="#42", now=clock())
        record = store.get_plan(PLAN_ID)
        assert record is not None
        report = run_emit(store, tracker, record, clock=clock)
        assert not report.complete
        assert report.failure is not None
        assert tracker.created == []


class TestCrashRecovery:
    def test_crash_mid_phase_one_resumes_without_duplicates(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        record = approved(store, clock, three_item_plan())
        tracker.fail_after_creates = 2

        report = run_emit(store, tracker, record, clock=clock)
        assert not report.complete
        assert report.created == 2
        assert store.plan_status(PLAN_ID) is PlanStatus.EMITTING  # no rollback
        assert any(e.kind == "plan_emit_failed" for e in store.events_after(0))
        assert any("could not finish" in text for key, text in tracker.comments if key == "#42")

        tracker.fail_after_creates = None
        retry = run_emit(store, tracker, record, clock=clock)
        assert retry.complete
        assert retry.created == 1  # only the missing item
        assert len(tracker.created) == 3  # never five

    def test_created_but_unrecorded_window_closed_by_adoption(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        record = approved(store, clock, three_item_plan())
        tracker.record_created_items = False  # die between create and ledger row

        report = run_emit(store, tracker, record, clock=clock)
        assert not report.complete
        assert store.emitted_items(PLAN_ID) == []  # the ledger missed it
        marked = [i for i in tracker.items if parse_body(i.body).plan_marker is not None]
        assert len(marked) == 1  # ...but the ticket exists

        tracker.record_created_items = True
        retry = run_emit(store, tracker, record, clock=clock)
        assert retry.complete
        assert retry.adopted == 1
        assert retry.created == 2
        marked = [i for i in tracker.items if parse_body(i.body).plan_marker is not None]
        assert len(marked) == 3  # adopted, not duplicated

    def test_crash_mid_phase_two_resumes_edges_only(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        record = approved(store, clock, three_item_plan())
        tracker.fail_after_body_updates = 1

        report = run_emit(store, tracker, record, clock=clock)
        assert not report.complete
        assert report.created == 3

        tracker.fail_after_body_updates = None
        retry = run_emit(store, tracker, record, clock=clock)
        assert retry.complete
        assert retry.created == 0
        assert retry.edges_written == 2  # only the remaining items
        assert len(tracker.body_updates) == 3

    def test_crash_before_status_flip_completes_with_no_item_writes(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        # Everything memoised — tickets exist, ledger full — but the crash
        # landed before the status flip. The re-run only flips and comments.
        plan = three_item_plan()
        record = approved(store, clock, plan)
        for item in plan.items:
            key = f"#9{item.index}"
            body = render_child_body(
                item.body, plan_id=PLAN_ID, item_index=item.index, scope=item.scope
            )
            tracker.items.append(
                TrackerItem(provider="github", external_key=key, title=item.title, body=body)
            )
            store.record_emitted_item(PLAN_ID, item.index, external_key=key, now=clock())
            store.mark_item_edges_written(PLAN_ID, item.index, now=clock())
            store.mark_item_mirrored(PLAN_ID, item.index, now=clock())

        report = run_emit(store, tracker, record, clock=clock)

        assert report.complete
        assert tracker.created == []
        assert tracker.body_updates == []
        assert tracker.mirrored == []
        assert store.plan_status(PLAN_ID) is PlanStatus.EMITTED


class TestAnomalies:
    def test_conflicting_marker_aborts(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        record = approved(store, clock, three_item_plan())
        store.record_emitted_item(PLAN_ID, 0, external_key="#900", now=clock())
        tracker.items.append(
            TrackerItem(
                provider="github",
                external_key="#901",
                title="impostor",
                body=f"x\n\ntf-plan: {PLAN_ID}/0",
            )
        )
        report = run_emit(store, tracker, record, clock=clock)
        assert not report.complete
        assert any(e.kind == "plan_emit_anomaly" for e in store.events_after(0))

    def test_mirror_failure_never_blocks_completion(
        self, store: Store, clock: FakeClock, tracker: FakeTracker
    ) -> None:
        record = approved(store, clock, three_item_plan())
        tracker.fail_mirror = True
        report = run_emit(store, tracker, record, clock=clock)
        assert report.complete
        assert report.mirrored == 0
        assert store.plan_status(PLAN_ID) is PlanStatus.EMITTED
        failures = [e for e in store.events_after(0) if e.kind == "plan_mirror_failed"]
        assert len(failures) == 2
        assert all(entry.mirrored_at is None for entry in store.emitted_items(PLAN_ID))


class TestClosedTrackerItems:
    def test_closing_a_duplicate_is_the_supported_recovery(
        self, store: Store, tracker: FakeTracker, clock: FakeClock
    ) -> None:
        # The concurrent-emit anomaly tells the operator to close the
        # duplicate; the sweep must then ignore the closed ticket instead of
        # raising the two-claimants anomaly forever.
        plan = approved(store, clock, three_item_plan())
        store.record_emitted_item(plan.plan_id, 0, external_key="#900", now=clock())
        tracker.items.append(
            TrackerItem(
                provider="github",
                external_key="#900",
                title="Scaffold",
                body=render_child_body("Create it.", plan_id=plan.plan_id, item_index=0),
            )
        )
        tracker.items.append(
            TrackerItem(
                provider="github",
                external_key="#901",
                title="dup",
                body=render_child_body("dup", plan_id=plan.plan_id, item_index=0),
                closed=True,
            )
        )
        report = run_emit(store, tracker, plan, clock=clock)
        assert report.failure is None

    def test_a_closed_orphan_is_never_adopted(
        self, store: Store, tracker: FakeTracker, clock: FakeClock
    ) -> None:
        # Crash window, then a human closed the partial as junk: adopting it
        # would wire downstream work to a ticket nobody will ever run.
        plan = approved(store, clock, three_item_plan())
        tracker.items.append(
            TrackerItem(
                provider="github",
                external_key="#902",
                title="junk",
                body=render_child_body("junk", plan_id=plan.plan_id, item_index=0),
                closed=True,
            )
        )
        report = run_emit(store, tracker, plan, clock=clock)
        assert report.adopted == 0
        entries = {e.item_index: e.external_key for e in store.emitted_items(plan.plan_id)}
        assert entries[0] != "#902"  # freshly created, not the closed junk


class TestMirrorPhaseFailures:
    def test_store_failure_during_mirroring_is_routed_to_the_failure_surface(
        self, store: Store, tracker: FakeTracker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = approved(store, clock, three_item_plan())

        def boom(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("disk full")

        monkeypatch.setattr(store, "mark_item_mirrored", boom)
        report = run_emit(store, tracker, plan, clock=clock)
        assert report.failure is not None
        assert "disk full" in report.failure
        kinds = [e.kind for e in store.events_after(0)]
        assert "plan_emit_failed" in kinds

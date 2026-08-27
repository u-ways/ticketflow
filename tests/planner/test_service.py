"""Planner service turns (ADR-0014): resumable, validated on every
revision, rejecting the turn — never the plan — on failure."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakeRunner, FakeSynthesizer, FakeTracker
from tests.orchestrator.conftest import FakeClock, FakeWorkspaces
from ticketflow.config import Config
from ticketflow.domain.errors import (
    PlanTurnRefused,
    PlanValidationError,
    UnknownEpic,
)
from ticketflow.domain.plan import PlanStatus
from ticketflow.planner.approval import consume_plan_intents
from ticketflow.planner.grounding import grounding_workspace_id
from ticketflow.planner.schema import Plan, PlanEdge, PlanItem
from ticketflow.planner.service import Planner
from ticketflow.planner.yaml_io import dump_plan, load_plan, plan_filename
from ticketflow.ports.tracker import TrackerItem
from ticketflow.store.store import Store


def build_plan(plan_id: str, *, epic_key: str = "#42", edges: bool = True) -> Plan:
    return Plan(
        plan_id=plan_id,
        epic_key=epic_key,
        items=(
            PlanItem(index=0, title="Scaffold", body="Create the package."),
            PlanItem(index=1, title="CLI", body="Add a CLI."),
        ),
        edges=(
            (PlanEdge(upstream=0, downstream=1, confidence=0.9, evidence="imports"),)
            if edges
            else ()
        ),
    )


class Harness:
    def __init__(self, tmp_path: Path, config: Config, *, yolo: bool = False) -> None:
        self.store = Store.open(tmp_path / "planner.db")
        self.tracker = FakeTracker()
        self.tracker.items.append(
            TrackerItem(provider="github", external_key="#42", title="Epic", body="Make it so.")
        )
        self.runner = FakeRunner()
        self.synthesizer = FakeSynthesizer()
        self.workspaces = FakeWorkspaces(tmp_path / "ws")
        self.clock = FakeClock()
        self.config = config
        self.planner = Planner(
            store=self.store,
            tracker=self.tracker,
            runner=self.runner,
            synthesizer=self.synthesizer,
            workspaces=self.workspaces,
            config=config,
            repo_exists=lambda: True,
            clock=self.clock,
            sleep=lambda seconds: self.clock.advance(int(seconds)),
            yolo=yolo,
        )

    def script_grounding(self, plan_id: str, brief: str = "# Brief\n") -> None:
        path = self.workspaces.root / grounding_workspace_id(plan_id) / "1"
        path.mkdir(parents=True, exist_ok=True)
        (path / "brief.md").write_text(brief, encoding="utf-8")
        self.runner.script_exit(grounding_workspace_id(plan_id), 1)

    def to_review(self) -> str:
        """Drive a fresh harness to in_review with revision 1."""
        plan = self.planner.ingest("#42")
        self.script_grounding(plan.plan_id)
        self.planner.ground("#42")
        self.synthesizer.script(build_plan(plan.plan_id))
        self.planner.synthesize("#42")
        return plan.plan_id

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def h(tmp_path: Path, config: Config) -> Iterator[Harness]:
    harness = Harness(tmp_path, config)
    yield harness
    harness.close()


class TestIngest:
    def test_creates_a_plan_once(self, h: Harness) -> None:
        first = h.planner.ingest("#42")
        second = h.planner.ingest("#42")
        assert first.plan_id == second.plan_id
        assert first.status is PlanStatus.INGESTED

    def test_unknown_epic_refused_before_any_write(self, h: Harness) -> None:
        with pytest.raises(UnknownEpic, match="#99"):
            h.planner.ingest("#99")
        assert h.store.list_plans() == []


class TestSynthesize:
    def test_first_proposal_lands_in_review_with_artifact(self, h: Harness) -> None:
        plan_id = h.to_review()
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        assert plan.status is PlanStatus.IN_REVIEW
        assert plan.current_revision == 1
        path = h.config.plans_dir / plan_filename("#42")
        assert path.is_file()
        revision = h.store.get_plan_revision(plan_id, 1)
        assert revision is not None
        assert path.read_text(encoding="utf-8") == revision.yaml  # file mirrors the blob
        kinds = [e.kind for e in h.store.events_after(0)]
        assert "plan_proposed" in kinds

    def test_identity_change_rejects_the_turn(self, h: Harness) -> None:
        plan = h.planner.ingest("#42")
        h.script_grounding(plan.plan_id)
        h.planner.ground("#42")
        h.synthesizer.script(build_plan("b" * 12))  # wrong plan_id
        with pytest.raises(PlanValidationError, match="identity"):
            h.planner.synthesize("#42")
        refreshed = h.store.get_plan(plan.plan_id)
        assert refreshed is not None
        assert refreshed.status is PlanStatus.SYNTHESIS
        assert refreshed.current_revision == 0

    def test_wrong_status_refused(self, h: Harness) -> None:
        h.planner.ingest("#42")
        with pytest.raises(PlanTurnRefused, match="ingested"):
            h.planner.synthesize("#42")


class TestRevise:
    def test_valid_revision_becomes_revision_two(self, h: Harness) -> None:
        plan_id = h.to_review()
        revised = build_plan(plan_id, edges=False)
        h.synthesizer.script(revised)
        assert h.planner.revise("#42", "drop the edge") == 2
        blob = h.store.get_plan_revision(plan_id, 2)
        assert blob is not None
        assert load_plan(blob.yaml).edges == ()
        path = h.config.plans_dir / plan_filename("#42")
        assert path.read_text(encoding="utf-8") == blob.yaml
        request = h.synthesizer.requests[-1]
        assert "drop the edge" in getattr(request, "feedback", "")

    def test_failing_turn_leaves_previous_revision_standing(self, h: Harness) -> None:
        plan_id = h.to_review()
        h.synthesizer.script(PlanValidationError("synthesis did not converge"))
        with pytest.raises(PlanValidationError):
            h.planner.revise("#42", "impossible ask")
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        assert plan.current_revision == 1  # the turn was rejected, not the plan
        kinds = [e.kind for e in h.store.events_after(0)]
        assert "plan_validation_failed" in kinds

    def test_semantically_invalid_output_rejects_the_turn(self, h: Harness) -> None:
        plan_id = h.to_review()
        cyclic = Plan(
            plan_id=plan_id,
            epic_key="#42",
            items=build_plan(plan_id).items,
            edges=(
                PlanEdge(upstream=0, downstream=1, confidence=0.9, evidence="e"),
                PlanEdge(upstream=1, downstream=0, confidence=0.9, evidence="e"),
            ),
        )
        h.synthesizer.script(cyclic)
        with pytest.raises(PlanValidationError, match="cycle"):
            h.planner.revise("#42", "add a cycle")
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        assert plan.current_revision == 1


class TestValidateFile:
    def test_hand_edit_becomes_a_human_edit_revision(self, h: Harness) -> None:
        plan_id = h.to_review()
        path = h.config.plans_dir / plan_filename("#42")
        edited = dump_plan(build_plan(plan_id, edges=False))
        path.write_text(edited, encoding="utf-8")
        assert h.planner.validate_file("#42") == 2
        blob = h.store.get_plan_revision(plan_id, 2)
        assert blob is not None
        assert blob.source == "human_edit"

    def test_unchanged_file_records_nothing(self, h: Harness) -> None:
        h.to_review()
        assert h.planner.validate_file("#42") is None

    def test_broken_file_rejected_and_left_on_disk(self, h: Harness) -> None:
        plan_id = h.to_review()
        path = h.config.plans_dir / plan_filename("#42")
        path.write_text("items: [unclosed", encoding="utf-8")
        with pytest.raises(PlanValidationError):
            h.planner.validate_file("#42")
        assert path.read_text(encoding="utf-8") == "items: [unclosed"  # left for fixing
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        assert plan.current_revision == 1

    def test_refused_after_approval(self, h: Harness) -> None:
        # Approved but NOT yet emitted: the no-mutation-after-approval
        # window (ADR-0014). Consume the approval without running emit.
        plan_id = h.to_review()
        h.planner.request_approval("#42")
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        consume_plan_intents(h.store, plan, clock=h.clock)
        assert h.store.plan_status(plan_id) is PlanStatus.EMITTING
        with pytest.raises(PlanTurnRefused, match="only editable in review"):
            h.planner.validate_file("#42")
        with pytest.raises(PlanTurnRefused, match="expected in_review"):
            h.planner.revise("#42", "too late")


class TestApprovalRequests:
    def test_approval_intent_pins_revision_and_digest(self, h: Harness) -> None:
        plan_id = h.to_review()
        assert h.planner.request_approval("#42") is not None
        [intent] = h.store.unprocessed_intents()
        assert intent.intent_type == "plan_approve"
        assert intent.node_id is None
        assert intent.payload["plan_id"] == plan_id
        assert intent.payload["revision"] == 1
        assert len(intent.payload["yaml_sha256"]) == 64

    def test_double_approval_dedupes_on_external_id(self, h: Harness) -> None:
        h.to_review()
        assert h.planner.request_approval("#42") is not None
        assert h.planner.request_approval("#42") is None

    def test_approval_ingests_pending_hand_edits_first(self, h: Harness) -> None:
        plan_id = h.to_review()
        path = h.config.plans_dir / plan_filename("#42")
        path.write_text(dump_plan(build_plan(plan_id, edges=False)), encoding="utf-8")
        h.planner.request_approval("#42")
        [intent] = h.store.unprocessed_intents()
        assert intent.payload["revision"] == 2  # what is on disk is what gets approved

    def test_rejection_is_applied_in_the_same_turn(self, h: Harness) -> None:
        # Rejection has no later natural turn, so the intent is written AND
        # consumed here — recorded through the table, applied immediately.
        plan_id = h.to_review()
        h.planner.request_rejection("#42", "wrong decomposition")
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        assert plan.status is PlanStatus.DISCARDED
        assert plan.discard_reason == "wrong decomposition"
        assert h.store.unprocessed_intents() == []

    def test_replan_after_rejection_via_ingest(self, h: Harness) -> None:
        first = h.to_review()
        h.planner.request_rejection("#42", "wrong shape")
        second = h.planner.ingest("#42")
        assert second.plan_id != first
        assert second.status is PlanStatus.INGESTED

    def test_pending_rejection_applied_on_next_ingest(self, h: Harness) -> None:
        # A reject intent written by another surface (not the CLI turn) is
        # consumed by the next planner turn, then re-planning proceeds.
        plan_id = h.to_review()
        h.store.add_intent(
            intent_type="plan_reject",
            source="tracker",
            node_id=None,
            payload={"plan_id": plan_id, "reason": "board veto"},
            external_id=f"plan_reject:{plan_id}",
            now=h.clock(),
        )
        fresh = h.planner.ingest("#42")
        assert fresh.plan_id != plan_id
        assert h.store.plan_status(plan_id) is PlanStatus.DISCARDED

    def test_stale_revision_approval_refused_up_front(self, h: Harness) -> None:
        plan_id = h.to_review()
        h.synthesizer.script(build_plan(plan_id, edges=False))
        h.planner.revise("#42", "prune")  # current is now revision 2
        with pytest.raises(PlanTurnRefused, match="not the latest"):
            h.planner.request_approval("#42", revision=1)
        assert h.store.unprocessed_intents() == []  # no doomed intent written


class TestEmitTurn:
    def test_emit_before_approval_refused(self, h: Harness) -> None:
        h.to_review()
        with pytest.raises(PlanTurnRefused, match="approve"):
            h.planner.emit("#42")

    def test_approve_then_emit_completes(self, h: Harness) -> None:
        plan_id = h.to_review()
        h.planner.request_approval("#42")
        report = h.planner.emit("#42")
        assert report.complete
        assert len(report.child_keys) == 2
        plan = h.store.get_plan(plan_id)
        assert plan is not None
        assert plan.status is PlanStatus.EMITTED
        assert h.store.unprocessed_intents() == []  # the approval was consumed


class TestYolo:
    def test_new_chains_approval_and_emission(self, tmp_path: Path, config: Config) -> None:
        h = Harness(tmp_path, config, yolo=True)
        try:
            plan = h.planner.ingest("#42")
            h.script_grounding(plan.plan_id)
            h.synthesizer.script(build_plan(plan.plan_id))
            final = h.planner.new("#42")
            assert final.status is PlanStatus.EMITTED
            assert len(h.tracker.created) == 2
            # The approval still flowed through the intents table (ADR-0004,
            # ADR-0013), and every artifact was written regardless of yolo.
            assert (h.config.plans_dir / plan_filename("#42")).is_file()
            assert h.store.unprocessed_intents() == []
            assert h.runner.started[0].policy.yolo is True
        finally:
            h.close()

"""CLI tests: read-only projections and intent-writing commands (ADR-0004)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ticketflow
from ticketflow.cli.app import app
from ticketflow.cli.factory import open_store
from ticketflow.config import load_config
from ticketflow.domain.model import NodeState

runner = CliRunner()
T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "ticketflow.toml"
    path.write_text(
        f"""
state_dir = "{tmp_path / ".ticketflow"}"

[tracker]
provider = "github"
repo = "o/r"

[codehost]
repo = "o/r"
"""
    )
    return path


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert ticketflow.__version__ in result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "ticketflow" in result.output


class TestInit:
    def test_writes_starter_config(self, tmp_path: Path) -> None:
        target = tmp_path / "ticketflow.toml"
        result = runner.invoke(app, ["init", str(target)])
        assert result.exit_code == 0
        config = load_config(target.parent / "ticketflow.toml")
        assert config.runner.name == "claude"

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        target = tmp_path / "ticketflow.toml"
        target.write_text("existing")
        result = runner.invoke(app, ["init", str(target)])
        assert result.exit_code == 1
        assert target.read_text() == "existing"


class TestMissingConfig:
    def test_status_without_config_exits_2(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["status", "--config", str(tmp_path / "nope.toml")])
        assert result.exit_code == 2


class TestReadOnlyViews:
    def test_status_lists_nodes_with_refs(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        store = open_store(load_config(config_path))
        store.insert_node(node_id="abc", title="Build it", body="", state=NodeState.READY, now=T0)
        store.link_external("abc", provider="github", external_key="#7")
        store.close()
        result = runner.invoke(app, ["status", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "abc" in result.output
        assert "#7" in result.output
        assert "ready" in result.output

    def test_escalations_show_reason_and_resolution_hint(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        store = open_store(load_config(config_path))
        store.insert_node(
            node_id="abc",
            title="Broken",
            body="",
            state=NodeState.ESCALATED,
            blocked_reason="wall-clock timeout",
            now=T0,
        )
        store.close()
        result = runner.invoke(app, ["escalations", "--config", str(config_path)])
        assert "wall-clock timeout" in result.output
        assert "ticketflow retry abc" in result.output

    def test_events_tail(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        store = open_store(load_config(config_path))
        store.append_event("dispatched", now=T0, node_id="abc", attempt=1)
        store.close()
        result = runner.invoke(app, ["events", "--config", str(config_path)])
        assert "dispatched" in result.output
        assert "node=abc" in result.output


class TestIntentCommands:
    def test_retry_with_feedback_writes_intent(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        result = runner.invoke(
            app,
            ["retry", "abc", "--config", str(config_path), "--feedback", "Mind the gap."],
        )
        assert result.exit_code == 0
        store = open_store(load_config(config_path))
        pending = store.unprocessed_intents()
        store.close()
        assert len(pending) == 1
        assert pending[0].intent_type == "retry"
        assert pending[0].node_id == "abc"
        assert pending[0].payload == {"feedback": "Mind the gap."}
        assert pending[0].source == "cli"

    def test_global_resume_has_no_node(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        runner.invoke(app, ["resume", "--config", str(config_path)])
        store = open_store(load_config(config_path))
        pending = store.unprocessed_intents()
        store.close()
        assert pending[0].intent_type == "resume"
        assert pending[0].node_id is None

    def test_cancel_and_unblock(self, tmp_path: Path) -> None:
        config_path = write_config(tmp_path)
        runner.invoke(app, ["cancel", "abc", "--config", str(config_path)])
        runner.invoke(app, ["unblock", "abc", "--config", str(config_path)])
        store = open_store(load_config(config_path))
        kinds = [i.intent_type for i in store.unprocessed_intents()]
        store.close()
        assert kinds == ["cancel", "unblock"]


PLAN_ID = "a3f8c2d91b04"


def write_planner_config(tmp_path: Path, *, model: bool = True) -> Path:
    path = tmp_path / "ticketflow.toml"
    model_line = 'synthesis_model = "test-model"' if model else ""
    path.write_text(
        f"""
state_dir = "{tmp_path / ".ticketflow"}"
plans_dir = "{tmp_path / "plans"}"

[tracker]
provider = "github"
repo = "o/r"

[codehost]
repo = "o/r"

[planner]
{model_line}
"""
    )
    return path


def seed_plan_in_review(config_path: Path, *, edges: bool = False) -> None:
    from ticketflow.domain.plan import PlanStatus
    from ticketflow.planner.schema import Plan, PlanEdge, PlanItem
    from ticketflow.planner.yaml_io import dump_plan, write_plan_file

    cfg = load_config(config_path)
    store = open_store(cfg)
    plan = Plan(
        plan_id=PLAN_ID,
        epic_key="#42",
        items=(
            PlanItem(index=0, title="Scaffold", body="Create it."),
            PlanItem(index=1, title="CLI", body="Wrap it."),
        ),
        edges=(PlanEdge(upstream=0, downstream=1, confidence=0.9, evidence="imports"),)
        if edges
        else (),
        unevidenced_edges=(PlanEdge(upstream=1, downstream=0, confidence=0.2),) if edges else (),
    )
    store.create_plan(plan_id=PLAN_ID, provider="github", epic_key="#42", now=T0)
    for status in (PlanStatus.GROUNDING, PlanStatus.SYNTHESIS, PlanStatus.IN_REVIEW):
        store.set_plan_status(PLAN_ID, status, now=T0)
    yaml_text = dump_plan(plan)
    store.add_plan_revision(PLAN_ID, yaml_text=yaml_text, source="synthesis", now=T0)
    write_plan_file(cfg.plans_dir, "#42", yaml_text)
    store.close()


def seed_plan_in_synthesis(config_path: Path) -> None:
    from ticketflow.domain.plan import PlanStatus

    cfg = load_config(config_path)
    store = open_store(cfg)
    store.create_plan(plan_id=PLAN_ID, provider="github", epic_key="#42", now=T0)
    for status in (PlanStatus.GROUNDING, PlanStatus.SYNTHESIS):
        store.set_plan_status(PLAN_ID, status, now=T0)
    store.set_plan_brief(PLAN_ID, "# Brief\nSeams.", now=T0)
    store.close()


class TestPlanCli:
    def test_missing_synthesis_model_refuses_only_the_synthesis_turn(self, tmp_path: Path) -> None:
        # Model-free turns (approve, emit, ...) still build a working
        # planner; the factory's refusing synthesizer fires only when a
        # turn actually synthesizes.
        import pytest as _pytest

        from ticketflow.cli.factory import build_planner, open_store
        from ticketflow.domain.errors import PlanTurnRefused
        from ticketflow.planner.synthesis import SynthesisRequest

        config_path = write_planner_config(tmp_path, model=False)
        cfg = load_config(config_path)
        store = open_store(cfg)
        try:
            planner = build_planner(cfg, store)
            synthesizer = planner._synthesizer
            with _pytest.raises(PlanTurnRefused, match="synthesis_model"):
                synthesizer.synthesize(
                    SynthesisRequest(
                        plan_id="a" * 12,
                        epic_key="#42",
                        epic_title="t",
                        epic_body="b",
                        brief="brief",
                    )
                )
        finally:
            store.close()

    def test_show_without_state(self, tmp_path: Path) -> None:
        config_path = write_planner_config(tmp_path)
        result = runner.invoke(app, ["plan", "show", "#42", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "No state yet" in result.output

    def test_show_lists_items_and_edges(self, tmp_path: Path) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        result = runner.invoke(app, ["plan", "show", "#42", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "Scaffold" in result.output
        assert "in_review" in result.output

    def test_list_shows_every_plan(self, tmp_path: Path) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        result = runner.invoke(app, ["plan", "list", "--config", str(config_path)])
        assert PLAN_ID in result.output

    def test_approve_writes_intent_once(self, tmp_path: Path) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        first = runner.invoke(app, ["plan", "approve", "#42", "--config", str(config_path)])
        assert first.exit_code == 0
        assert "plan_approve" in first.output
        second = runner.invoke(app, ["plan", "approve", "#42", "--config", str(config_path)])
        assert second.exit_code == 0
        assert "already recorded" in second.output
        store = open_store(load_config(config_path))
        pending = store.unprocessed_intents()
        store.close()
        assert len(pending) == 1
        assert pending[0].intent_type == "plan_approve"
        assert pending[0].payload["plan_id"] == PLAN_ID

    def test_validate_records_a_hand_edit(self, tmp_path: Path) -> None:
        from ticketflow.planner.yaml_io import plan_filename

        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        cfg = load_config(config_path)
        path = cfg.plans_dir / plan_filename("#42")
        path.write_text(path.read_text().replace("Wrap it.", "Wrap it well."), encoding="utf-8")
        result = runner.invoke(app, ["plan", "validate", "#42", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "revision 2" in result.output

    def test_emit_before_approval_exits_1(self, tmp_path: Path) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        result = runner.invoke(app, ["plan", "emit", "#42", "--config", str(config_path)])
        assert result.exit_code == 1
        assert "approve" in result.output

    def test_approve_then_emit_with_fake_tracker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.fakes import FakeTracker
        from ticketflow.cli import factory
        from ticketflow.ports.tracker import TrackerItem

        fake = FakeTracker()
        fake.items.append(
            TrackerItem(provider="github", external_key="#42", title="Epic", body="Do.")
        )
        monkeypatch.setattr(factory, "build_tracker", lambda _cfg: fake)

        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        runner.invoke(app, ["plan", "approve", "#42", "--config", str(config_path)])
        result = runner.invoke(app, ["plan", "emit", "#42", "--config", str(config_path)])
        assert result.exit_code == 0, result.output
        assert "emitted 2 child items" in result.output
        assert len(fake.created) == 2

    def test_failed_emit_exits_3_and_resumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.fakes import FakeTracker
        from ticketflow.cli import factory
        from ticketflow.ports.tracker import TrackerItem

        fake = FakeTracker()
        fake.items.append(
            TrackerItem(provider="github", external_key="#42", title="Epic", body="Do.")
        )
        fake.fail_after_creates = 1
        monkeypatch.setattr(factory, "build_tracker", lambda _cfg: fake)

        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        runner.invoke(app, ["plan", "approve", "#42", "--config", str(config_path)])
        failed = runner.invoke(app, ["plan", "emit", "#42", "--config", str(config_path)])
        assert failed.exit_code == 3
        assert "resume" in failed.output

        fake.fail_after_creates = None
        retry = runner.invoke(app, ["plan", "emit", "#42", "--config", str(config_path)])
        assert retry.exit_code == 0, retry.output
        assert len(fake.created) == 2  # adopted, never duplicated


def fake_planner_builder(tmp_path: Path, scripted: list[object]) -> object:
    """A build_planner replacement wiring the CLI to fakes (no network)."""
    from collections.abc import Callable

    from tests.fakes import FakeRunner, FakeSynthesizer, FakeTracker
    from tests.orchestrator.conftest import FakeWorkspaces
    from ticketflow.cli.factory import utc_now
    from ticketflow.config import Config
    from ticketflow.planner.schema import Plan
    from ticketflow.planner.service import Planner
    from ticketflow.ports.tracker import TrackerItem
    from ticketflow.store.store import Store

    tracker = FakeTracker()
    tracker.items.append(
        TrackerItem(provider="github", external_key="#42", title="Epic", body="Do.")
    )
    synthesizer = FakeSynthesizer()
    for plan in scripted:
        assert isinstance(plan, Plan)
        synthesizer.script(plan)

    def build(
        config: Config,
        store: Store,
        *,
        yolo: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> Planner:
        return Planner(
            store=store,
            tracker=tracker,
            runner=FakeRunner(),
            synthesizer=synthesizer,
            workspaces=FakeWorkspaces(tmp_path / "ws"),
            config=config,
            repo_exists=lambda: True,
            clock=clock,
            yolo=yolo,
        )

    return build


class TestPlanCliSurface:
    def test_edit_applies_editor_changes_as_a_revision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        editor = tmp_path / "editor.sh"
        editor.write_text('#!/bin/sh\nsed -i "s/Wrap it./Wrap it well./" "$1"\n')
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))
        result = runner.invoke(app, ["plan", "edit", "#42", "--config", str(config_path)])
        assert result.exit_code == 0, result.output
        assert "revision 2" in result.output

    def test_edit_seeds_a_missing_file_and_reports_no_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ticketflow.planner.yaml_io import plan_filename

        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        cfg = load_config(config_path)
        path = cfg.plans_dir / plan_filename("#42")
        path.unlink()
        monkeypatch.setenv("EDITOR", "true")  # a no-op editor
        result = runner.invoke(app, ["plan", "edit", "#42", "--config", str(config_path)])
        assert result.exit_code == 0, result.output
        assert path.is_file()  # re-seeded from the stored revision blob
        assert "no changes" in result.output

    def test_edit_rejects_a_broken_edit_and_leaves_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        editor = tmp_path / "breaker.sh"
        editor.write_text('#!/bin/sh\necho "items: [unclosed" > "$1"\n')
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))
        result = runner.invoke(app, ["plan", "edit", "#42", "--config", str(config_path)])
        assert result.exit_code == 1
        assert "not valid YAML" in result.output

    def test_reject_discards_the_plan_in_the_same_turn(self, tmp_path: Path) -> None:
        from ticketflow.domain.plan import PlanStatus

        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        result = runner.invoke(
            app,
            ["plan", "reject", "#42", "--reason", "wrong split", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        store = open_store(load_config(config_path))
        assert store.plan_status(PLAN_ID) is PlanStatus.DISCARDED
        assert store.unprocessed_intents() == []
        store.close()

    def test_revise_records_a_new_revision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ticketflow.cli import factory
        from ticketflow.planner.schema import Plan, PlanItem

        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path)
        revised = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(
                PlanItem(index=0, title="Scaffold", body="Create it."),
                PlanItem(index=1, title="CLI", body="Wrap it well."),
            ),
        )
        monkeypatch.setattr(factory, "build_planner", fake_planner_builder(tmp_path, [revised]))
        result = runner.invoke(
            app,
            ["plan", "revise", "#42", "--feedback", "polish", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert "revision 2" in result.output

    def test_new_resumes_from_synthesis_and_surfaces_review_hints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ticketflow.cli import factory
        from ticketflow.planner.schema import Plan, PlanItem

        config_path = write_planner_config(tmp_path)
        seed_plan_in_synthesis(config_path)
        proposal = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(PlanItem(index=0, title="Scaffold", body="Create it."),),
        )
        monkeypatch.setattr(factory, "build_planner", fake_planner_builder(tmp_path, [proposal]))
        result = runner.invoke(app, ["plan", "new", "#42", "--config", str(config_path)])
        assert result.exit_code == 0, result.output
        assert "in_review" in result.output
        assert "plan approve" in result.output  # the review instructions

    def test_show_renders_edges_unevidenced_and_emission(self, tmp_path: Path) -> None:
        config_path = write_planner_config(tmp_path)
        seed_plan_in_review(config_path, edges=True)
        store = open_store(load_config(config_path))
        store.record_emitted_item(PLAN_ID, 0, external_key="#101", now=T0)
        store.mark_item_edges_written(PLAN_ID, 0, now=T0)
        store.close()
        result = runner.invoke(app, ["plan", "show", "#42", "--config", str(config_path)])
        assert result.exit_code == 0, result.output
        assert "ascending by confidence" in result.output
        assert "0 -> 1" in result.output
        assert "WITHOUT evidence" in result.output
        assert "emission:" in result.output
        assert "edges written" in result.output

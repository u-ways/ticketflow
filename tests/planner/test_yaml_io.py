"""The plan YAML file: round-trips through the validators, survives hand
edits, and rejects broken files as a rejected turn (ADR-0014, spec §13.5).
"""

from pathlib import Path

import pytest

from ticketflow.domain.errors import PlanValidationError
from ticketflow.planner.schema import Plan, PlanEdge, PlanItem
from ticketflow.planner.yaml_io import dump_plan, load_plan, plan_filename, write_plan_file

PLAN_ID = "a3f8c2d91b04"


def make_plan() -> Plan:
    return Plan(
        plan_id=PLAN_ID,
        epic_key="#42",
        notes="Bootstrap first.",
        items=(
            PlanItem(index=0, title="Scaffold", body="Create the package.\n\nWith tests.\n"),
            PlanItem(index=1, title="CLI", body="Add a CLI.", scope=("src/cli/", "tests/")),
        ),
        edges=(PlanEdge(upstream=0, downstream=1, confidence=0.9, evidence="CLI imports pkg"),),
        unevidenced_edges=(PlanEdge(upstream=1, downstream=0, confidence=0.2),),
    )


class TestRoundTrip:
    def test_dump_then_load_is_identity(self) -> None:
        plan = make_plan()
        assert load_plan(dump_plan(plan)) == plan

    def test_dump_is_deterministic(self) -> None:
        assert dump_plan(make_plan()) == dump_plan(make_plan())

    def test_dump_carries_confidence_comments(self) -> None:
        text = dump_plan(make_plan())
        assert "# confidence 0.90" in text
        assert "prune" in text  # the reviewer's job is pruning (spec §13.2)

    def test_hand_edit_survives_reload(self) -> None:
        # A human pruning the unevidenced edge in $EDITOR is a valid turn.
        text = dump_plan(make_plan())
        edited = text[: text.index("unevidenced_edges:")].rstrip() + "\n"
        plan = load_plan(edited)
        assert plan.unevidenced_edges == ()
        assert len(plan.edges) == 1


class TestRejectedTurns:
    def test_unparseable_yaml_rejected(self) -> None:
        with pytest.raises(PlanValidationError, match="not valid YAML"):
            load_plan("items: [unclosed")

    def test_non_mapping_rejected(self) -> None:
        with pytest.raises(PlanValidationError, match="mapping"):
            load_plan("- just\n- a\n- list\n")

    def test_schema_violation_rejected(self) -> None:
        # A hand edit dropping an item that an edge still references.
        text = dump_plan(make_plan()).replace("index: 1", "index: 5")
        with pytest.raises(PlanValidationError, match="validation"):
            load_plan(text)


class TestPlanFiles:
    def test_github_key_sanitised(self) -> None:
        assert plan_filename("#42") == "42.yaml"

    def test_jira_key_passes_through(self) -> None:
        assert plan_filename("PROJ-7") == "PROJ-7.yaml"

    def test_write_creates_dir_and_file(self, tmp_path: Path) -> None:
        path = write_plan_file(tmp_path / "plans", "#42", "plan_id: x\n")
        assert path == tmp_path / "plans" / "42.yaml"
        assert path.read_text(encoding="utf-8") == "plan_id: x\n"


class TestHostileRoundTrips:
    def test_yaml_meaningful_strings_survive(self) -> None:
        # Titles and bodies that YAML wants to reinterpret must round-trip.
        plan = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(
                PlanItem(index=0, title="yes: or no", body="- not a list\n*not bold*\n"),
                PlanItem(index=1, title="  spaced  ", body="unicode — ✓ — and 'quotes'"),
            ),
            edges=(
                PlanEdge(
                    upstream=0,
                    downstream=1,
                    confidence=0.5,
                    evidence="a very long citation " * 20,
                ),
            ),
            notes="notes with\nnewlines and #hash",
        )
        dumped = dump_plan(plan)
        reloaded = load_plan(dumped)
        assert reloaded == plan
        assert dump_plan(reloaded) == dumped  # dumped form is the fixed point

    def test_unknown_keys_reject_the_turn(self) -> None:
        # A hand-edit typo must fail loudly, never silently drop data.
        plan = Plan(
            plan_id=PLAN_ID,
            epic_key="#42",
            items=(PlanItem(index=0, title="Only", body="Item."),),
        )
        text = dump_plan(plan).replace("items:", "itemz:", 1)
        with pytest.raises(PlanValidationError):
            load_plan(text)

"""Planner prompts (ADR-0014): pure builders carrying the mechanical
instructions the phases depend on."""

from ticketflow.planner.prompts import (
    build_grounding_prompt,
    render_revision_input,
    render_synthesis_input,
)


class TestGroundingPrompt:
    def test_carries_epic_brief_instruction_and_date(self) -> None:
        prompt = build_grounding_prompt(
            epic_title="Build the tool",
            epic_body="Make it so.",
            repo="o/r",
            greenfield=False,
            today="2026-08-27",
        )
        assert "Build the tool" in prompt
        assert "Make it so." in prompt
        assert "brief.md" in prompt
        assert "2026-08-27" in prompt
        assert "change nothing" in prompt  # research, not implementation

    def test_direct_upstream_handoffs_included(self) -> None:
        prompt = build_grounding_prompt(
            epic_title="T",
            epic_body="B",
            repo="o/r",
            greenfield=False,
            upstream_handoffs={"abc123": "Touched src/x."},
            today="2026-08-27",
        )
        assert "abc123" in prompt
        assert "Touched src/x." in prompt

    def test_greenfield_states_the_bootstrap_default(self) -> None:
        prompt = build_grounding_prompt(
            epic_title="T", epic_body="", repo="o/r", greenfield=True, today="2026-08-27"
        )
        assert "does not exist" in prompt
        assert "branch protection" in prompt

    def test_empty_body_is_labelled(self) -> None:
        prompt = build_grounding_prompt(
            epic_title="T", epic_body="  ", repo="o/r", greenfield=False, today="2026-08-27"
        )
        assert "(no further description)" in prompt


class TestSynthesisInputs:
    def test_synthesis_input_carries_epic_and_brief(self) -> None:
        text = render_synthesis_input(
            epic_title="T", epic_body="B", brief="# Brief", greenfield=False
        )
        assert "# Epic: T" in text
        assert "# Brief" in text
        assert "bootstrap" not in text

    def test_greenfield_adds_the_bootstrap_default(self) -> None:
        text = render_synthesis_input(
            epic_title="T", epic_body="B", brief="# Brief", greenfield=True
        )
        assert "bootstrap" in text

    def test_revision_input_carries_all_three_parts(self) -> None:
        text = render_revision_input(
            current_plan_yaml="plan_id: x", feedback="drop the edge", brief="# Brief"
        )
        assert "plan_id: x" in text
        assert "drop the edge" in text
        assert "# Brief" in text

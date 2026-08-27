"""PydanticAISynthesizer against pydantic-ai's test models — no network.

TestModel scripts a fixed structured output; FunctionModel scripts an
invalid-then-valid sequence to prove the retry-on-validation-failure loop
(spec §13.3's Synthesis→Synthesis edge, ADR-0014).
"""

import json
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from ticketflow.adapters.pydanticai_synthesis import PydanticAISynthesizer
from ticketflow.domain.errors import PlanValidationError
from ticketflow.planner.synthesis import RevisionRequest, SynthesisRequest
from ticketflow.planner.validate import semantic_errors

PLAN_ID = "a3f8c2d91b04"


def draft_args(*, edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "items": [
            {"index": 0, "title": "Scaffold", "body": "Create the package."},
            {"index": 1, "title": "CLI", "body": "Add a CLI."},
        ],
        "edges": edges
        if edges is not None
        else [{"upstream": 0, "downstream": 1, "confidence": 0.9, "evidence": "CLI imports pkg"}],
        "unevidenced_edges": [],
        "notes": "",
    }


def synthesis_request() -> SynthesisRequest:
    return SynthesisRequest(
        plan_id=PLAN_ID,
        epic_key="#42",
        epic_title="Build the tool",
        epic_body="Make it so.",
        brief="# Brief\nFindings.",
    )


class TestSynthesize:
    def test_valid_output_becomes_a_plan_with_injected_identity(self) -> None:
        synthesizer = PydanticAISynthesizer(
            model=TestModel(custom_output_args=draft_args()),
            validate=semantic_errors,
        )
        plan = synthesizer.synthesize(synthesis_request())
        assert plan.plan_id == PLAN_ID  # identity is the caller's, never the model's
        assert plan.epic_key == "#42"
        assert [item.title for item in plan.items] == ["Scaffold", "CLI"]
        assert plan.edges[0].evidence == "CLI imports pkg"

    def test_semantic_failure_retries_until_valid(self) -> None:
        # First call proposes a cycle; the ModelRetry feedback drives a fix.
        calls: list[int] = []

        def flaky(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls.append(len(messages))
            assert info.output_tools  # structured output goes through a tool
            bad = draft_args(
                edges=[
                    {"upstream": 0, "downstream": 1, "confidence": 0.9, "evidence": "e"},
                    {"upstream": 1, "downstream": 0, "confidence": 0.9, "evidence": "e"},
                ]
            )
            args = bad if len(calls) == 1 else draft_args()
            return ModelResponse(
                parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=json.dumps(args))]
            )

        synthesizer = PydanticAISynthesizer(
            model=FunctionModel(flaky), validate=semantic_errors, max_retries=3
        )
        plan = synthesizer.synthesize(synthesis_request())
        assert len(calls) == 2  # one rejected turn, one valid one
        assert len(plan.edges) == 1

    def test_structural_failure_also_retries(self) -> None:
        calls: list[int] = []

        def flaky(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls.append(len(messages))
            assert info.output_tools
            bad = draft_args(
                edges=[{"upstream": 0, "downstream": 7, "confidence": 0.9, "evidence": "e"}]
            )
            args = bad if len(calls) == 1 else draft_args()
            return ModelResponse(
                parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=json.dumps(args))]
            )

        synthesizer = PydanticAISynthesizer(
            model=FunctionModel(flaky), validate=semantic_errors, max_retries=3
        )
        plan = synthesizer.synthesize(synthesis_request())
        assert len(calls) == 2
        assert plan.edges[0].downstream == 1

    def test_never_valid_output_exhausts_retries(self) -> None:
        def always_bad(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            del messages
            assert info.output_tools
            bad = draft_args(
                edges=[
                    {"upstream": 0, "downstream": 1, "confidence": 0.9, "evidence": "e"},
                    {"upstream": 1, "downstream": 0, "confidence": 0.9, "evidence": "e"},
                ]
            )
            return ModelResponse(
                parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=json.dumps(bad))]
            )

        synthesizer = PydanticAISynthesizer(
            model=FunctionModel(always_bad), validate=semantic_errors, max_retries=2
        )
        # The vendor exception is translated at the boundary (ADR-0002).
        with pytest.raises(PlanValidationError, match="did not converge"):
            synthesizer.synthesize(synthesis_request())


class TestRevise:
    def test_revision_is_a_stateless_turn_over_the_inputs(self) -> None:
        seen_inputs: list[str] = []

        def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            for message in messages:
                for part in message.parts:
                    content = getattr(part, "content", None)
                    if isinstance(content, str):
                        seen_inputs.append(content)
            assert info.output_tools
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name=info.output_tools[0].name, args=json.dumps(draft_args()))
                ]
            )

        synthesizer = PydanticAISynthesizer(model=FunctionModel(capture), validate=semantic_errors)
        plan = synthesizer.revise(
            RevisionRequest(
                plan_id=PLAN_ID,
                epic_key="#42",
                current_plan_yaml="plan_id: a3f8c2d91b04\n",
                feedback="drop the unevidenced edge",
                brief="# Brief",
            )
        )
        assert plan.plan_id == PLAN_ID
        joined = "\n".join(seen_inputs)
        assert "drop the unevidenced edge" in joined
        assert "plan_id: a3f8c2d91b04" in joined

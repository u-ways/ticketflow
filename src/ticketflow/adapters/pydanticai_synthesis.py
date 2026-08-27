"""PydanticAI synthesis adapter (ADR-0014; permitted by the ADR-0002
revision).

The only planning module that touches a model API. It implements the
planner-internal :class:`~ticketflow.planner.synthesis.PlanSynthesizer`
Protocol: typed output against a draft schema, with schema-or-semantic
failures retried inside the loop via ``ModelRetry`` (spec §13.3's
Synthesis→Synthesis edge). Capability differences between backends — native
constrained decoding versus prompted structured output — are handled by
pydantic-ai's model profiles (spec §13.4: branch on capability, not vendor).

The semantic validator is injected as a callable: it lives in
:mod:`ticketflow.planner.validate`, which imports the graph primitives, and
adapters may not import ``graph``/``store``/``orchestrator`` (ADR-0002).
The model identifier always arrives from config (ADR-0011); prompt content
lives in :mod:`ticketflow.planner.prompts`, not here.
"""

from collections.abc import Callable

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models import Model

from ticketflow.domain.errors import PlanValidationError
from ticketflow.planner.prompts import (
    REVISION_INSTRUCTIONS,
    SYNTHESIS_INSTRUCTIONS,
    render_revision_input,
    render_synthesis_input,
)
from ticketflow.planner.schema import Plan
from ticketflow.planner.synthesis import PlanDraft, RevisionRequest, SynthesisRequest


class PydanticAISynthesizer:
    """PlanSynthesizer over any pydantic-ai model string or instance."""

    def __init__(
        self,
        *,
        model: str | Model,
        validate: Callable[[Plan], tuple[str, ...]],
        max_retries: int = 3,
    ) -> None:
        self._model = model
        self._validate = validate
        self._max_retries = max_retries

    def synthesize(self, request: SynthesisRequest) -> Plan:
        return self._run(
            SYNTHESIS_INSTRUCTIONS,
            render_synthesis_input(
                epic_title=request.epic_title,
                epic_body=request.epic_body,
                brief=request.brief,
                greenfield=request.greenfield,
            ),
            plan_id=request.plan_id,
            epic_key=request.epic_key,
        )

    def revise(self, request: RevisionRequest) -> Plan:
        return self._run(
            REVISION_INSTRUCTIONS,
            render_revision_input(
                current_plan_yaml=request.current_plan_yaml,
                feedback=request.feedback,
                brief=request.brief,
            ),
            plan_id=request.plan_id,
            epic_key=request.epic_key,
        )

    def _run(self, instructions: str, user_input: str, *, plan_id: str, epic_key: str) -> Plan:
        agent: Agent[None, PlanDraft] = Agent(
            self._model,
            output_type=PlanDraft,
            instructions=instructions,
            retries=self._max_retries,
        )
        validate = self._validate

        @agent.output_validator
        def _valid(draft: PlanDraft) -> PlanDraft:
            plan = _assemble(plan_id, epic_key, draft)
            errors = validate(plan)
            if errors:
                raise ModelRetry("the plan failed validation: " + "; ".join(errors))
            return draft

        try:
            result = agent.run_sync(user_input)
        except AgentRunError as exc:
            # Translate at the boundary: callers know the domain error, not
            # the vendor one (ADR-0002). Retries were already spent inside.
            raise PlanValidationError(f"synthesis did not converge: {exc}") from exc
        return _assemble(plan_id, epic_key, result.output)


def _assemble(plan_id: str, epic_key: str, draft: PlanDraft) -> Plan:
    """Attach identity and run the Plan's structural validators.

    Inside the output validator a structural failure becomes a ModelRetry;
    at the final assembly the draft has already passed, so this cannot fire.
    """
    try:
        return draft.assemble(plan_id=plan_id, epic_key=epic_key)
    except ValueError as exc:
        raise ModelRetry(f"the plan failed structural validation: {exc}") from exc

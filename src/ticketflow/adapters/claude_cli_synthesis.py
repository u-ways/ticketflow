"""Claude CLI synthesis adapter (ADR-0014; ADR-0002 revision).

The second `PlanSynthesizer` implementation: the headless ``claude`` CLI in
print mode, the same auth story as the runner (ADR-0011) — no API key, so a
subscription-authenticated operator can run the whole planner. Constrained
decoding is unavailable through the CLI, so this is the spec's
retry-on-validation-failure path (spec §13.4): the draft is requested as
strict JSON, validated, and a failing turn is retried with the error fed
back. The model identifier always arrives from config (ADR-0011).
"""

import json
import re
import subprocess
from collections.abc import Callable

from pydantic import ValidationError

from ticketflow.domain.errors import PlanValidationError
from ticketflow.planner.prompts import (
    OUTPUT_CONTRACT,
    REVISION_INSTRUCTIONS,
    SYNTHESIS_INSTRUCTIONS,
    render_revision_input,
    render_synthesis_input,
)
from ticketflow.planner.schema import Plan
from ticketflow.planner.synthesis import PlanDraft, RevisionRequest, SynthesisRequest

_FENCE_RE = re.compile(r"^```[a-z]*\n(?P<body>.*)\n```$", re.DOTALL)


class ClaudeCliSynthesizer:
    """PlanSynthesizer over ``claude -p --output-format json``."""

    def __init__(
        self,
        *,
        validate: Callable[[Plan], tuple[str, ...]],
        model: str | None = None,
        binary: str = "claude",
        max_retries: int = 3,
        timeout_seconds: int = 600,
    ) -> None:
        self._validate = validate
        self._model = model
        self._binary = binary
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds

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
        error: str | None = None
        for _ in range(self._max_retries + 1):
            prompt = f"{instructions}\n\n{OUTPUT_CONTRACT}\n\n{user_input}"
            if error is not None:
                prompt += (
                    f"\n\n## Your previous answer failed validation — fix and re-emit\n{error}"
                )
            try:
                draft = PlanDraft.model_validate_json(_extract_json(self._invoke(prompt)))
                plan = draft.assemble(plan_id=plan_id, epic_key=epic_key)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
                continue
            semantic = self._validate(plan)
            if semantic:
                error = "; ".join(semantic)
                continue
            return plan
        raise PlanValidationError(f"synthesis did not converge: {error}")

    def _invoke(self, prompt: str) -> str:
        command = [self._binary, "-p", prompt, "--output-format", "json"]
        if self._model is not None:
            command += ["--model", self._model]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=self._timeout_seconds
        )
        if result.returncode != 0:
            raise PlanValidationError(
                f"synthesis CLI exited {result.returncode}: {result.stderr.strip()[-300:]}"
            )
        return result.stdout


def _extract_json(stdout: str) -> str:
    """The draft JSON out of the CLI's ``--output-format json`` envelope.

    Tolerates a markdown-fenced answer — the retry loop handles anything
    worse.
    """
    envelope = json.loads(stdout)
    text = str(envelope.get("result", "")).strip()
    if match := _FENCE_RE.match(text):
        return match.group("body")
    return text

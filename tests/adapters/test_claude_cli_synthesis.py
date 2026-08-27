"""Claude CLI synthesis adapter tests: a scripted fake binary, no network."""

import json
import stat
from pathlib import Path

import pytest

from ticketflow.adapters.claude_cli_synthesis import ClaudeCliSynthesizer
from ticketflow.domain.errors import PlanValidationError
from ticketflow.planner.synthesis import RevisionRequest, SynthesisRequest
from ticketflow.planner.validate import semantic_errors

PLAN_ID = "ab12cd34ef56"

GOOD_DRAFT = {
    "items": [
        {"index": 0, "title": "Scaffold", "body": "Create the package.", "scope": ["src/"]},
        {"index": 1, "title": "Docs", "body": "Write the README."},
    ],
    "edges": [{"upstream": 0, "downstream": 1, "confidence": 0.9, "evidence": "docs need code"}],
    "notes": "",
}

BAD_DRAFT = {
    # Edge references a nonexistent item: structural validation must fail
    # and the adapter must retry with the error fed back.
    "items": [{"index": 0, "title": "Only", "body": "One item."}],
    "edges": [{"upstream": 0, "downstream": 5, "confidence": 0.5, "evidence": "wrong"}],
}


def make_fake_claude(tmp_path: Path, *drafts: dict[str, object]) -> str:
    """A fake ``claude`` that answers with each draft in turn.

    Emits the real CLI's ``--output-format json`` envelope; records every
    argv to ``calls.log`` for assertions.
    """
    responses = tmp_path / "responses"
    responses.mkdir()
    for n, draft in enumerate(drafts):
        envelope = {"type": "result", "result": json.dumps(draft)}
        (responses / f"{n}.json").write_text(json.dumps(envelope))
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        # One line per invocation, newlines flattened, so calls() can assert
        # per-call content.
        f"printf '%s ' \"$@\" | tr '\\n' ' ' >> {tmp_path}/calls.log\n"
        f"echo >> {tmp_path}/calls.log\n"
        f"COUNT_FILE={tmp_path}/count\n"
        'N=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\n'
        f'cat {responses}/"$N".json\n'
        'echo $((N + 1)) > "$COUNT_FILE"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text().splitlines()


def synthesizer(binary: str, model: str | None = None) -> ClaudeCliSynthesizer:
    return ClaudeCliSynthesizer(validate=semantic_errors, model=model, binary=binary, max_retries=2)


def request() -> SynthesisRequest:
    return SynthesisRequest(
        plan_id=PLAN_ID,
        epic_key="#1",
        epic_title="Build it",
        epic_body="An epic.",
        brief="The brief.",
    )


class TestSynthesize:
    def test_happy_path_attaches_identity(self, tmp_path: Path) -> None:
        fake = make_fake_claude(tmp_path, GOOD_DRAFT)
        plan = synthesizer(fake).synthesize(request())
        assert plan.plan_id == PLAN_ID
        assert plan.epic_key == "#1"
        assert [item.title for item in plan.items] == ["Scaffold", "Docs"]
        assert plan.edges[0].evidence == "docs need code"

    def test_invalid_draft_is_retried_with_the_error_fed_back(self, tmp_path: Path) -> None:
        fake = make_fake_claude(tmp_path, BAD_DRAFT, GOOD_DRAFT)
        plan = synthesizer(fake).synthesize(request())
        assert len(plan.items) == 2
        recorded = calls(tmp_path)
        assert len(recorded) == 2
        assert "nonexistent item" in recorded[1]  # the retry carries the error

    def test_exhausted_retries_raise(self, tmp_path: Path) -> None:
        fake = make_fake_claude(tmp_path, BAD_DRAFT, BAD_DRAFT, BAD_DRAFT)
        with pytest.raises(PlanValidationError, match="did not converge"):
            synthesizer(fake).synthesize(request())

    def test_fenced_json_is_tolerated(self, tmp_path: Path) -> None:
        fenced = {"type": "result", "result": f"```json\n{json.dumps(GOOD_DRAFT)}\n```"}
        responses = tmp_path / "responses"
        responses.mkdir()
        (responses / "0.json").write_text(json.dumps(fenced))
        script = tmp_path / "fake-claude"
        script.write_text(f"#!/bin/sh\ncat {responses}/0.json\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        plan = synthesizer(str(script)).synthesize(request())
        assert plan.plan_id == PLAN_ID

    def test_model_flag_only_when_configured(self, tmp_path: Path) -> None:
        fake = make_fake_claude(tmp_path, GOOD_DRAFT, GOOD_DRAFT)
        synthesizer(fake, model="sonnet").synthesize(request())
        assert "--model sonnet" in calls(tmp_path)[0]
        (tmp_path / "count").write_text("1")
        (tmp_path / "calls.log").unlink()
        synthesizer(fake).synthesize(request())
        assert "--model" not in calls(tmp_path)[0]

    def test_cli_failure_raises(self, tmp_path: Path) -> None:
        script = tmp_path / "fake-claude"
        script.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        with pytest.raises(PlanValidationError, match="exited 1"):
            synthesizer(str(script)).synthesize(request())


class TestRevise:
    def test_revision_uses_current_yaml_and_feedback(self, tmp_path: Path) -> None:
        fake = make_fake_claude(tmp_path, GOOD_DRAFT)
        plan = synthesizer(fake).revise(
            RevisionRequest(
                plan_id=PLAN_ID,
                epic_key="#1",
                current_plan_yaml="items: []",
                feedback="Drop the docs item.",
                brief="The brief.",
            )
        )
        assert plan.plan_id == PLAN_ID
        assert "Drop the docs item." in calls(tmp_path)[0]

"""Prompt construction tests (pure functions)."""

from ticketflow.orchestrator.prompts import build_feedback, build_prompt


class TestBuildPrompt:
    def test_worktree_prompt_carries_the_essentials(self) -> None:
        prompt = build_prompt(
            title="Add the API",
            body="Details here.",
            repo="o/r",
            branch="tf/abc",
            default_branch="main",
            bootstrap=False,
            scope_hints=("src/api/",),
            upstream_handoffs={"n0": "Introduced FooPort."},
            feedback="Mind the edge case.",
        )
        assert "Add the API" in prompt
        assert "tf/abc" in prompt
        assert "Introduced FooPort." in prompt
        assert "Mind the edge case." in prompt
        assert "src/api/" in prompt
        assert "handoff.md" in prompt
        assert "Do NOT merge" in prompt

    def test_bootstrap_prompt_has_no_pr_instructions(self) -> None:
        prompt = build_prompt(
            title="Create the repo",
            body="",
            repo="o/r",
            branch="tf/abc",
            default_branch="main",
            bootstrap=True,
        )
        assert "does not exist yet" in prompt
        assert "handoff.md" not in prompt
        assert "worktree" not in prompt

    def test_direct_handoffs_only_are_included_by_caller_contract(self) -> None:
        # The prompt renders exactly what it is given: transitivity control
        # lives at the call site, which passes direct upstreams only.
        prompt = build_prompt(
            title="T",
            body="",
            repo="o/r",
            branch="b",
            default_branch="main",
            bootstrap=False,
            upstream_handoffs={},
        )
        assert "Handoffs" not in prompt


class TestBuildFeedback:
    def test_batches_all_signal_kinds(self) -> None:
        text = build_feedback(
            failed_checks=("ci", "lint"),
            comments=[("rev", "a.py:3", "Rename it.")],
            changes_requested=True,
        )
        assert "ci" in text
        assert "lint" in text
        assert "Rename it." in text
        assert "requested changes" in text
        assert "Do not weaken" in text

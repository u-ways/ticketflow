"""Prompt construction for node dispatch. Pure functions.

The prompt carries the ticket, direct upstream handoffs only (never
transitive, ADR-0013), advisory scope hints (spec §12.6), and the mechanical
instructions the loop depends on: push the branch, write the handoff, never
merge. ticketflow adds no quality judgement here — the repo's gates judge the
result (ADR-0009).
"""

from collections.abc import Mapping, Sequence

HANDOFF_INSTRUCTIONS = """\
Before you finish, write a file named `handoff.md` at the workspace root: at
most 300 words covering files touched, interfaces introduced or changed,
decisions made and why, what you deliberately did NOT do, and known gotchas.
Downstream work reads this file; write it for the next engineer. Do NOT
commit `handoff.md` — it is a run artifact the orchestrator collects, not
part of the change."""


def build_prompt(
    *,
    title: str,
    body: str,
    repo: str,
    branch: str,
    default_branch: str,
    bootstrap: bool,
    scope_hints: Sequence[str] = (),
    upstream_handoffs: Mapping[str, str] | None = None,
    feedback: str | None = None,
) -> str:
    sections = [f"# Task: {title}", "", body.strip() or "(no further description)"]

    if upstream_handoffs:
        sections += ["", "## Handoffs from completed upstream work"]
        for upstream, handoff in sorted(upstream_handoffs.items()):
            sections += ["", f"### {upstream}", handoff.strip()]

    if feedback:
        sections += ["", "## Operator feedback", feedback.strip()]

    if scope_hints:
        joined = ", ".join(scope_hints)
        sections += [
            "",
            "## Expected scope (advisory)",
            f"This work is expected to touch: {joined}. This is guidance, not a fence.",
        ]

    if bootstrap:
        sections += [
            "",
            "## Delivery instructions",
            f"The target repository {repo} does not exist yet; your workspace is an "
            "empty directory. Create the project the task describes, initialise git, "
            f"create the GitHub repository {repo}, and push the initial history to "
            f"its default branch ({default_branch}). Do not create pull requests "
            "for this bootstrap work.",
        ]
    else:
        sections += [
            "",
            "## Delivery instructions",
            f"You are in a git worktree of {repo} on branch `{branch}`. Commit your "
            f"work to this branch and push it to origin as `{branch}`. Do NOT merge, "
            "do NOT push to the default branch, and do NOT open a pull request — "
            "the orchestrator opens it and the repository's own checks and "
            "reviewers judge it.",
            "",
            HANDOFF_INSTRUCTIONS,
        ]

    return "\n".join(sections) + "\n"


def build_feedback(
    *,
    failed_checks: Sequence[str],
    comments: Sequence[tuple[str, str | None, str]],
    changes_requested: bool,
) -> str:
    """Batch one settle's signals into a single feedback message (spec §9.2)."""
    parts: list[str] = [
        "The pull request for your work needs attention. Address ALL of the "
        "following, then commit and push to the same branch."
    ]
    if failed_checks:
        parts += ["", "Failing checks: " + ", ".join(failed_checks) + "."]
    if changes_requested:
        parts += ["", "A reviewer has requested changes."]
    if comments:
        parts += ["", "Unresolved review comments:"]
        for author, location, body in comments:
            where = f" ({location})" if location else ""
            parts.append(f"- {author}{where}: {body}")
    parts += [
        "",
        "Fix the underlying problems. Do not weaken tests, checks or lint "
        "configuration to get to green.",
    ]
    return "\n".join(parts)

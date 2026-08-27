"""Planner prompt construction (ADR-0014). Pure functions.

Mirrors :mod:`ticketflow.orchestrator.prompts`: keyword-only builders joining
sections. The grounding prompt carries the epic, the direct-upstream handoffs
only (never transitive, ADR-0013), and the mechanical instruction the phase
depends on — write ``brief.md`` at the workspace root, the same file-capture
pattern as handoffs. Synthesis instructions live here too, so prompt content
stays out of the adapter (ADR-0002).
"""

from collections.abc import Mapping

BRIEF_INSTRUCTIONS = """\
Write your research brief to a file named `brief.md` at the workspace root,
in markdown. Cover: what the epic is really asking for; the relevant parts of
the codebase (paths, interfaces, conventions) with enough precision that a
planner could split the work; related past tickets and what they imply, with
their dates — discount stale ones explicitly; risks and unknowns; and any
natural seams for decomposition. Do NOT propose the decomposition itself and
do NOT write any other files — the brief is your only output."""

SYNTHESIS_INSTRUCTIONS = """\
You turn one epic plus a research brief into a plan: child items and the
dependency edges between them. Items must be independently deliverable and
carry acceptance criteria in their bodies. Item bodies must NOT contain
`depends-on:`, `scope:` or `tf-plan:` lines — dependencies are edges in the
plan, expected paths go in the item's scope field, and the emitter renders
both. Every edge carries a confidence between 0 and 1 and the evidence it
was drawn from, quoted or cited from the epic, the brief or a named source.
Propose an edge WITHOUT evidence only in unevidenced_edges. Do not invent
plausible dependencies: a missed edge is recoverable in review, a fabricated
one silently serializes the graph (over-prediction is the known failure
mode, so propose fewer, better-evidenced edges)."""

OUTPUT_CONTRACT = """\
Answer with ONLY a JSON object — no prose, no markdown fences — of the form:
{"items": [{"index": 0, "title": "...", "body": "...", "scope": ["path/"]}],
 "edges": [{"upstream": 0, "downstream": 1, "confidence": 0.8,
            "evidence": "quoted or cited source"}],
 "unevidenced_edges": [], "notes": ""}
Item indices are contiguous from 0. `scope` and `notes` may be omitted.
Every entry in `edges` must cite evidence; a proposal without evidence goes
in `unevidenced_edges`."""

REVISION_INSTRUCTIONS = """\
You revise an existing plan in response to reviewer feedback. Apply the
feedback and re-derive its consequences: removing an edge may free items to
run in parallel, splitting an item needs new acceptance criteria and its
edges rewired. Keep everything the feedback does not touch unchanged —
same items, same indices, same wording. The same schema rules apply:
evidence on every edge, no grammar lines in item bodies."""


def build_grounding_prompt(
    *,
    epic_title: str,
    epic_body: str,
    repo: str,
    greenfield: bool,
    upstream_handoffs: Mapping[str, str] | None = None,
    today: str,
) -> str:
    sections = [
        f"# Research an epic before planning: {epic_title}",
        "",
        epic_body.strip() or "(no further description)",
        "",
        f"Today's date is {today}. Attach dates to anything you retrieve and "
        "discount stale information explicitly.",
    ]

    if upstream_handoffs:
        sections += ["", "## Handoffs from completed upstream work"]
        for upstream, handoff in sorted(upstream_handoffs.items()):
            sections += ["", f"### {upstream}", handoff.strip()]

    if greenfield:
        sections += [
            "",
            "## Context",
            f"The target repository {repo} does not exist yet; your workspace is "
            "an empty directory, so ground the brief in the epic and its linked "
            "material alone. Note in the brief that bootstrap work — create the "
            "repository, add CI, turn on branch protection — belongs upstream of "
            "everything else, so the gates exist before the work they judge.",
        ]
    else:
        sections += [
            "",
            "## Context",
            f"Your workspace is a read-only checkout of {repo}. Explore it — "
            "code, docs, configuration — and read whatever else the epic links "
            "to. You are researching, not implementing: change nothing.",
        ]

    sections += ["", BRIEF_INSTRUCTIONS]
    return "\n".join(sections) + "\n"


def render_synthesis_input(*, epic_title: str, epic_body: str, brief: str, greenfield: bool) -> str:
    sections = [
        f"# Epic: {epic_title}",
        "",
        epic_body.strip() or "(no further description)",
        "",
        "## Research brief",
        "",
        brief.strip(),
    ]
    if greenfield:
        sections += [
            "",
            "The target repository does not exist yet: emit the bootstrap work "
            "(create the repo, add CI, add branch protection) upstream of "
            "everything else. This is a strong default the reviewer can undo.",
        ]
    return "\n".join(sections) + "\n"


def render_revision_input(*, current_plan_yaml: str, feedback: str, brief: str) -> str:
    sections = [
        "# Current plan",
        "",
        "```yaml",
        current_plan_yaml.strip(),
        "```",
        "",
        "## Reviewer feedback",
        "",
        feedback.strip(),
        "",
        "## Research brief (for reference)",
        "",
        brief.strip(),
    ]
    return "\n".join(sections) + "\n"

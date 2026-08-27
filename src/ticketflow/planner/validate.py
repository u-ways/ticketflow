"""Semantic plan validation (ADR-0014).

Checks that need more than the schema: cycle rejection via the same
``graphlib.TopologicalSorter.prepare()`` primitive the scheduler uses
(``graph.ready.detect_cycles``), and item bodies that must not smuggle
grammar lines the emit path owns (ADR-0007). Runs on every revision —
synthesis output, agent revisions and human hand-edits alike (spec §13.5).
"""

from ticketflow.domain.errors import DependencyCycle
from ticketflow.domain.parser import parse_body
from ticketflow.graph.ready import detect_cycles
from ticketflow.planner.schema import Plan


def semantic_errors(plan: Plan) -> tuple[str, ...]:
    """Every semantic problem with the plan; empty means valid."""
    errors: list[str] = []

    edges = {
        (str(edge.upstream), str(edge.downstream))
        for edge in (*plan.edges, *plan.unevidenced_edges)
    }
    try:
        detect_cycles(edges)
    except DependencyCycle as exc:
        errors.append(str(exc))

    for item in plan.items:
        parsed = parse_body(item.body)
        if parsed.depends_on or parsed.scope or parsed.plan_marker is not None or parsed.issues:
            errors.append(
                f"item {item.index} body carries grammar lines "
                "(depends-on/scope/tf-plan); dependencies belong in edges and "
                "paths in the item's scope field — the emit path renders them"
            )
        for path in item.scope:
            # Everything the renderer would refuse is refused here, so an
            # approved plan can never get stuck failing emission (ADR-0014):
            # the turn is rejected instead.
            if not path.strip() or "," in path or "\n" in path:
                errors.append(f"item {item.index} has a malformed scope path: {path!r}")

    return tuple(errors)

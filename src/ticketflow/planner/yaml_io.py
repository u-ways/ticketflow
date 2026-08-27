"""Plan file I/O (ADR-0014): ``plans/<epic-key>.yaml``.

The YAML file is the human review surface — diffed, versioned, hand-editable
with ``$EDITOR`` (spec §13.5) — while the byte-exact revision blob in SQLite
stays the truth (ADR-0003). Confidence and evidence are real schema fields
(validators need them; comments cannot be validated); a per-edge end-of-line
comment repeats the confidence for scanability, and reloading a hand-edited
file goes straight back through the Pydantic validators.
"""

import re
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from ticketflow.domain.errors import PlanValidationError
from ticketflow.planner.schema import Plan, PlanEdge

_EDGES_HELP = "edges are proposals: prune what the evidence does not support (spec 13.2)"
_UNEVIDENCED_HELP = "proposed without citable evidence: promote with evidence, or delete"


def dump_plan(plan: Plan) -> str:
    """Render a plan as commented YAML. Deterministic for a given plan."""
    root = CommentedMap()
    root["plan_id"] = plan.plan_id
    root["epic_key"] = plan.epic_key
    if plan.notes:
        root["notes"] = _scalar(plan.notes)

    items = CommentedSeq()
    for item in plan.items:
        entry = CommentedMap()
        entry["index"] = item.index
        entry["title"] = item.title
        entry["body"] = _scalar(item.body)
        if item.scope:
            entry["scope"] = list(item.scope)
        items.append(entry)
    root["items"] = items

    root["edges"] = _edge_seq(plan.edges)
    root.yaml_add_eol_comment(_EDGES_HELP, key="edges")
    if plan.unevidenced_edges:
        root["unevidenced_edges"] = _edge_seq(plan.unevidenced_edges)
        root.yaml_add_eol_comment(_UNEVIDENCED_HELP, key="unevidenced_edges")

    stream = StringIO()
    _yaml().dump(root, stream)
    return stream.getvalue()


def load_plan(text: str) -> Plan:
    """Parse and validate a plan file; every failure is a rejected turn.

    Raises :class:`PlanValidationError` for YAML that does not parse, is not
    a mapping, or fails the schema's structural validators — the previous
    revision stands (spec §13.5 rule 1).
    """
    try:
        data: Any = _yaml().load(text)
    except Exception as exc:
        raise PlanValidationError(f"plan file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanValidationError("plan file must be a YAML mapping")
    try:
        return Plan.model_validate(data)
    except ValueError as exc:
        raise PlanValidationError(f"plan file failed validation: {exc}") from exc


def plan_filename(epic_key: str) -> str:
    """Deterministic, filesystem-safe file name for an epic key.

    ``"#42"`` -> ``42.yaml``; ``"PROJ-7"`` -> ``PROJ-7.yaml``.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", epic_key.removeprefix("#"))
    return f"{safe}.yaml"


def write_plan_file(plans_dir: Path, epic_key: str, text: str) -> Path:
    """Write the working copy; the stored revision blob remains the truth."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / plan_filename(epic_key)
    path.write_text(text, encoding="utf-8")
    return path


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.width = 100
    return yaml


def _edge_seq(edges: tuple[PlanEdge, ...]) -> CommentedSeq:
    seq = CommentedSeq()
    for edge in edges:
        entry = CommentedMap()
        entry["upstream"] = edge.upstream
        entry["downstream"] = edge.downstream
        entry["confidence"] = edge.confidence
        entry["evidence"] = _scalar(edge.evidence)
        entry.yaml_add_eol_comment(f"confidence {edge.confidence:.2f}", key="upstream")
        seq.append(entry)
    return seq


def _scalar(text: str) -> str:
    return LiteralScalarString(text) if "\n" in text else text

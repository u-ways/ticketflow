"""The plan schema (ADR-0014): items, edges with confidence and evidence.

Structural validation lives here as Pydantic validators: contiguous item
indices, edge endpoints that exist, no duplicate or self edges, confidence
bounds, and evidence required on every evidenced edge. Edges without citable
evidence are surfaced separately in ``unevidenced_edges`` — the reviewer's
main job is pruning (spec §13.2). Semantic checks that need the graph
primitives (cycles) live in :mod:`ticketflow.planner.validate`.
"""

import hashlib
import re
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PLAN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def derive_plan_id(provider: str, epic_key: str, created_at: datetime) -> str:
    """Deterministic plan identity, distinct per planning run.

    The timestamp keeps a re-plan of the same epic (after a rejection) from
    colliding with the discarded plan's idempotency keys and ``tf-plan:``
    markers. Derivation happens once, immediately before the plan row is
    inserted; afterwards the id is always read back from the store.
    """
    digest = hashlib.sha256(f"plan:{provider}:{epic_key}:{created_at.isoformat()}".encode())
    return digest.hexdigest()[:12]


class PlanItem(BaseModel):
    """One proposed child ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    title: str = Field(min_length=1)
    body: str
    scope: tuple[str, ...] = ()


class PlanEdge(BaseModel):
    """A proposed dependency: ``downstream`` depends on ``upstream``.

    Every edge carries a confidence value and the evidence it was drawn from
    (ADR-0014). Edges whose evidence is empty belong in the plan's
    ``unevidenced_edges`` list, never in ``edges``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    upstream: int = Field(ge=0)
    downstream: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""


class Plan(BaseModel):
    """A validated decomposition of one epic (ADR-0014)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    epic_key: str = Field(min_length=1)
    items: tuple[PlanItem, ...] = Field(min_length=1)
    edges: tuple[PlanEdge, ...] = ()
    unevidenced_edges: tuple[PlanEdge, ...] = ()
    notes: str = ""

    @model_validator(mode="after")
    def _structurally_valid(self) -> Self:
        errors: list[str] = []
        if not _PLAN_ID_RE.match(self.plan_id):
            errors.append(f"malformed plan id: {self.plan_id!r}")

        indices = [item.index for item in self.items]
        if sorted(indices) != list(range(len(indices))):
            errors.append(f"item indices must be contiguous from 0; got {sorted(indices)}")

        index_set = set(indices)
        seen: set[tuple[int, int]] = set()
        for kind, edges in (("edge", self.edges), ("unevidenced edge", self.unevidenced_edges)):
            for edge in edges:
                pair = (edge.upstream, edge.downstream)
                if edge.upstream == edge.downstream:
                    errors.append(f"self-{kind}: {edge.upstream} -> {edge.downstream}")
                if edge.upstream not in index_set or edge.downstream not in index_set:
                    errors.append(
                        f"{kind} references a nonexistent item: "
                        f"{edge.upstream} -> {edge.downstream}"
                    )
                if pair in seen:
                    errors.append(f"duplicate {kind}: {edge.upstream} -> {edge.downstream}")
                seen.add(pair)
        for edge in self.edges:
            if not edge.evidence.strip():
                errors.append(
                    f"edge {edge.upstream} -> {edge.downstream} has no cited evidence; "
                    "move it to unevidenced_edges"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self

"""The synthesis seam (ADR-0014): a Protocol, not a fourth port.

Synthesis is a pure transformation of (epic, brief) into a validated
:class:`~ticketflow.planner.schema.Plan`, and revision a pure transformation
of (current YAML, feedback, brief) — stateless turns, which is what lets a
days-long review resume from stored state with no resident process. The
production implementation is model-backed and lives under
``src/ticketflow/adapters/`` (ADR-0002); planner-core tests script
``FakeSynthesizer`` instead.
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from ticketflow.planner.schema import Plan, PlanEdge, PlanItem


class PlanDraft(BaseModel):
    """What a model authors: a plan minus the identity the caller owns."""

    items: tuple[PlanItem, ...]
    edges: tuple[PlanEdge, ...] = ()
    unevidenced_edges: tuple[PlanEdge, ...] = ()
    notes: str = ""

    def assemble(self, *, plan_id: str, epic_key: str) -> Plan:
        """Attach identity and run the Plan's structural validators."""
        return Plan(
            plan_id=plan_id,
            epic_key=epic_key,
            items=self.items,
            edges=self.edges,
            unevidenced_edges=self.unevidenced_edges,
            notes=self.notes,
        )


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """Everything the first synthesis turn is a function of."""

    plan_id: str
    epic_key: str
    epic_title: str
    epic_body: str
    brief: str
    greenfield: bool = False


@dataclass(frozen=True, slots=True)
class RevisionRequest:
    """Everything a revision turn is a function of (spec §13.5)."""

    plan_id: str
    epic_key: str
    current_plan_yaml: str
    feedback: str
    brief: str


class PlanSynthesizer(Protocol):
    """Model-backed plan authoring behind a planner-internal seam."""

    def synthesize(self, request: SynthesisRequest) -> Plan: ...

    def revise(self, request: RevisionRequest) -> Plan: ...

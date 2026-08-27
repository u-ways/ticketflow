"""The offline planner (ADR-0014).

A separate phase in front of the scheduler: Grounding (a tool-using agent via
the RunnerPort writes a research brief) -> Synthesis (a pure transformation of
brief plus epic into a validated plan) -> human review (the plan YAML plus
revision turns) -> approval through the intents table -> idempotent emission
of child tickets through the TrackerPort.

No module in this package calls a model. The one model-backed seam,
:class:`~ticketflow.planner.synthesis.PlanSynthesizer`, is a Protocol whose
production implementation lives under ``src/ticketflow/adapters/`` (ADR-0002);
the scheduler never sees any of this — emitted children re-enter the core as
ordinary synced tracker items (ADR-0008).
"""

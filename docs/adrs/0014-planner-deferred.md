# ADR-0014: The planner is a separate offline phase (deferred)

- Status: Accepted
- Date: 2026-08-27
- Note: the design is accepted now; the implementation is deferred past M1.

## Context

Real epics arrive underspecified: humans omit dependencies, and title-only
tickets are common (spec §13). Something has to turn a messy tracker item into
a graph the deterministic scheduler (ADR-0008) can execute, without putting a
model inside the scheduling loop. The spec resolves this with a planner that
runs entirely before execution: it proposes a decomposition, a human approves
it, and the approved plan is emitted as ordinary tracker items with
`depends-on:` blocks (spec §13.1, ADR-0007).

Published benchmarks report F1 around 50% for LLM dependency inference on
realistic inputs, with models systematically over-predicting edges (spec
§13.2). A spurious edge causes unnecessary serialization — slow but correct —
while a missed edge causes unsafe parallelism, so the review gate must be
weighted towards pruning proposed edges.

The planner's design is settled now, but its implementation lands after M1.
This ADR records the design so that M1 code cannot accidentally paint it out.

## Decision

The planner is a separate, offline phase in front of the scheduler. Its design
is accepted now; its implementation is deferred past M1.

- **An LLM authoring the graph is not an LLM executing it.** The planner
  proposes; a human approves; real child tickets with real `depends-on:`
  blocks are written back to the tracker (ADR-0007). From that moment the
  scheduler reads the graph exactly as it reads a human-authored one
  (ADR-0008). No model runs in the scheduling loop.
- **Two jobs carry two risk profiles** (spec §13.2). Enrichment — drafting
  missing descriptions and acceptance criteria — is low risk and gets normal
  review. Dependency inference is high risk and requires explicit per-edge
  confirmation: models over-predict edges, so the reviewer's main job is
  pruning. The plan schema must carry, per proposed edge, a confidence value
  and the evidence it was drawn from; edges without citable evidence are
  surfaced separately.
- **Implementation splits into two phases** (spec §13.4). Grounding is
  tool-using exploration and runs on the Agent SDK via `RunnerPort`
  (ADR-0011). Synthesis is a pure transformation of brief plus raw ticket into
  a validated plan: PydanticAI for the typed loop, Pydantic validators for the
  schema (every edge references an existing item; confidence present; evidence
  cited), and `graphlib.TopologicalSorter.prepare()` to reject cycles — the
  same primitive the scheduler uses. LangGraph is rejected: its durable
  `interrupt()`/resume duplicates the intents table (ADR-0004).
- **Approval uses the existing intents table** (ADR-0004). Plans live in
  SQLite (ADR-0003) plus `plans/<epic-key>.yaml`; the file is the artifact
  that gets diffed and versioned. No process stays resident across the review,
  which may span days — each turn resumes from stored state and exits.
- **Approval is all-or-nothing** (spec §13.7). No partial emit; the scheduler
  never starts against a graph still being edited.
- **Emission is idempotent and resumable.** Derive an idempotency key per item
  from plan id and item index; record each created item as it succeeds and
  skip existing ones on retry; create all items before any edges; do not mark
  the plan emitted until every item and edge exists. On permanent failure,
  leave the partials in place — tagged with the plan id, invisible to the
  scheduler, recoverable by re-running emit.
- **There is no mid-execution re-planning** (spec §13.6). The graph is
  materialized at approval and does not change underneath the scheduler. A
  node that reveals the decomposition was wrong escalates (ADR-0006) and a
  human decides whether to re-plan the remainder.
- **M1 must not foreclose any of the above.** Node creation must be possible
  from adapter sync alone; the intents model must be extensible to plan
  approval types later; the `plans/` directory and the plan tables are
  reserved for the planner.

## Consequences

- The scheduler stays deterministic and testable: it never sees a model, only
  a materialized graph, so ADR-0008's guarantees are untouched by planner
  quality.
- Plan durability comes for free from ADR-0003 and ADR-0004: a days-long
  review survives reboots and upgrades with no checkpointer dependency, and a
  human can hand-edit the YAML.
- Emission is the risky step and the design accepts that: twelve items is
  twelve tracker calls with no transaction, so correctness rests on
  idempotency keys and ordering rather than rollback. Orphaned partials are a
  deliberate, visible outcome of permanent failure.
- Deferral means M1 epics must be decomposed by hand, and every M1 design
  choice carries a latent obligation not to block the planner — a constraint
  that is easy to violate silently, which is why the review guidance below
  exists.
- No mid-execution re-planning means structural surprises stall the subtree
  until a human acts; that is accepted as the price of a stable graph.

## Review guidance

- Flag any M1 change that makes node creation depend on anything other than
  adapter sync (e.g. assuming nodes are only created by a planner or CLI),
  since emitted child tickets must enter as ordinary synced tracker items.
- Flag schema migrations or intent-handling code that closes off new intent
  types (e.g. an exhaustive enum with no extension point), which would block
  plan-approval intents later.
- Flag any new use of a `plans/` directory or tables named `plan*` for
  non-planner purposes — both are reserved by this ADR.
- Require any planner implementation PR to carry per-edge confidence and
  cited evidence in the plan schema, with validators rejecting edges that
  reference nonexistent items and `graphlib` rejecting cycles.
- Require plan approval to be written through the intents table (ADR-0004),
  not through a bespoke approval mechanism or an in-process checkpointer;
  flag any LangGraph dependency.
- Require emission code to derive an idempotency key per item, create items
  before edges, and contain no rollback/deletion path for partially emitted
  plans.
- Flag any code path that mutates graph structure (adds or removes edges or
  nodes) after a plan is approved, other than a human-resolved escalation.

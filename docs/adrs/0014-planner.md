# ADR-0014: The planner is a separate offline phase

- Status: Accepted
- Date: 2026-08-27
- Revision 2026-08-27: implementation landed; deferral language removed.
  The concrete shape: a `src/ticketflow/planner/` package — grounding via
  `RunnerPort` (brief captured as `brief.md`, the handoff file pattern),
  synthesis behind a planner-internal `PlanSynthesizer` Protocol with two
  adapter implementations (`pydanticai_synthesis`; `claude_cli_synthesis`,
  the headless-CLI auth story of ADR-0011, selected by
  `planner.synthesis_backend`) — with plan tables (ADR-0003 revision),
  digest-pinned `plan_approve`/`plan_reject` intents (ADR-0004 revision),
  `TrackerPort` widened for emission (ADR-0002 revision), and the
  `tf-plan:` marker (ADR-0007 revision) driving the scheduler's plan-hold.
  `plans/<epic-key>.yaml` is the committed working copy (SQLite stays
  truth); revision turns are stateless — the v1 review surface is the CLI
  plus `$EDITOR` (spec §13.5); the TUI session host remains future work.
  Accepted narrownesses: concurrent emit turns are unsupported (the ledger
  fails loudly, naming the duplicate to close — a CLOSED tracker item is
  neither adopted nor a claimant, so closing IS the recovery); phase 2
  rewrites every child body from the approved revision, overwriting
  crash-window human edits; a plan flipping to emitted mid-sync can leave
  one stale body for a tick. Escape hatches: `EMITTING → DISCARDED` exists
  solely to retract an approval while the emission ledger is still empty;
  an `unblock` intent may release a hold whose plan id the plans table has
  never heard of (a foreign or rebuilt database — there is no emission to
  wait for); re-planning an emitted epic requires an explicit `--re-plan`.

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

The design was recorded before M1 so that M1 code could not accidentally
paint it out; this revision lands the implementation.

## Decision

The planner is a separate, offline phase in front of the scheduler.

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
  tool-using exploration and runs on the runner adapters via `RunnerPort`
  (ADR-0011), under a wall-clock runaway guard (ADR-0010) with the brief
  captured from `brief.md` at the workspace root. Synthesis is a pure
  transformation of brief plus raw ticket into a validated plan: pydantic-ai
  for the typed loop behind the planner-internal `PlanSynthesizer` Protocol
  (its implementation is the one model-touching planning module and lives
  under `src/ticketflow/adapters/`, ADR-0002), Pydantic validators for the
  schema (every edge references an existing item; confidence present;
  evidence cited), and `graphlib.TopologicalSorter.prepare()` to reject
  cycles — the same primitive the scheduler uses. LangGraph is rejected: its
  durable `interrupt()`/resume duplicates the intents table (ADR-0004).
- **Approval uses the existing intents table** (ADR-0004). Plans live in
  SQLite (ADR-0003) plus `plans/<epic-key>.yaml`; the file is the artifact
  that gets diffed and versioned, and the byte-exact revision blob in SQLite
  is the truth the file is regenerated from. Every revision — synthesis
  output, agent revision turn, human hand-edit — is validated on entry, and
  a failing revision rejects the turn, never the plan. Approval pins one
  whole revision by number and content digest; a stale or edited-after-
  approval revision refuses to emit until re-approved. The
  `plan_approved` event records the first-proposal-versus-approved diff
  (spec §13.5 rule 3). No process stays resident across the review, which
  may span days — each turn resumes from stored state and exits.
- **Approval is all-or-nothing** (spec §13.7). No partial emit; the scheduler
  never starts against a graph still being edited. Children sync as ordinary
  nodes but are held Blocked by the orchestrator until their plan reads
  `emitted` — a hold neither the ready-set nor an `unblock` intent may
  bypass.
- **Emission is idempotent and resumable.** The idempotency key per item is
  the `plan_emitted_items` primary key `(plan id, item index)`; each created
  item is recorded as it succeeds and existing ones are skipped on retry; an
  adoption sweep re-reads the `tf-plan:` marker off the tracker to close the
  created-but-unrecorded crash window; all items are created before any
  edges; the plan is not marked emitted until every item and edge exists.
  Native dependency mirrors (Jira links, GitHub relationships or the board's
  "Blocked by" field) are written last, best-effort, and never block
  completion (ADR-0007). On permanent failure, leave the partials in place —
  tagged with the plan id via the marker, invisible to the scheduler,
  recoverable by re-running emit.
- **There is no mid-execution re-planning** (spec §13.6). The graph is
  materialized at approval and does not change underneath the scheduler. A
  node that reveals the decomposition was wrong escalates (ADR-0006) and a
  human decides whether to re-plan the remainder; a re-plan after rejection
  is a new plan row with a new plan id, so the discarded plan's markers and
  idempotency keys can never be adopted by mistake.
- **The core must not foreclose any of the above.** Node creation must be
  possible from adapter sync alone; the intents model must be extensible to
  plan approval types; the `plans/` directory and the plan tables are
  reserved for the planner. These were M1 obligations and are now permanent
  invariants the implementation relies on.

## Consequences

- The scheduler stays deterministic and testable: it never sees a model, only
  a materialized graph, so ADR-0008's guarantees are untouched by planner
  quality. Its whole planner surface is two deterministic behaviours: leave
  `plan_*` intents pending, and hold marker-bearing children until their
  plan is emitted.
- Plan durability comes for free from ADR-0003 and ADR-0004: a days-long
  review survives reboots and upgrades with no checkpointer dependency, and a
  human can hand-edit the YAML.
- Emission is the risky step and the design accepts that: twelve items is
  twelve tracker calls with no transaction, so correctness rests on
  idempotency keys, the marker adoption sweep, and ordering rather than
  rollback. Orphaned partials are a deliberate, visible outcome of permanent
  failure.
- Epics no longer need hand-decomposition. Review ergonomics are
  deliberately minimal at first — CLI plus `$EDITOR`, with the stateless
  `plan revise` turn as the conversational form — and the TUI session host
  can land later without touching plan state, because the plan is a file and
  approval is an intent.
- No mid-execution re-planning means structural surprises stall the subtree
  until a human acts; that is accepted as the price of a stable graph.

## Review guidance

- Flag any change that makes node creation depend on anything other than
  adapter sync (e.g. a planner or CLI code path inserting node or edge
  rows), since emitted child tickets must enter as ordinary synced tracker
  items.
- Flag schema migrations or intent-handling code that closes off new intent
  types (e.g. an exhaustive enum with no extension point), and any
  orchestrator change that consumes, reorders or events the `plan_*` intent
  namespace instead of leaving it pending for the planner turn (ADR-0004).
- Flag any new use of the `plans/` directory or tables named `plan*` for
  non-planner purposes — both are reserved by this ADR.
- Require the plan schema to carry per-edge confidence and cited evidence,
  with validators rejecting edges that reference nonexistent items and
  `graphlib` rejecting cycles; require every revision (synthesis, revise
  turn, hand-edit) to be validated on entry with a failing revision
  rejecting the turn, not the plan.
- Require plan approval to be written through the intents table (ADR-0004),
  not through a bespoke approval mechanism or an in-process checkpointer;
  flag any LangGraph dependency.
- Require emission code to derive its idempotency from the
  `(plan id, item index)` ledger key, create items before edges, and contain
  no rollback/deletion path for partially emitted plans.
- Flag any code path that mutates graph structure (adds or removes edges or
  nodes) after a plan is approved, other than a human-resolved escalation.
- Flag `pydantic_ai` imports outside `src/ticketflow/adapters/`, and any
  planner code invoked from the orchestrator tick — the planner runs only in
  its own CLI turns.
- Flag any path that marks a plan emitted before every item and edge exists,
  and any scheduler change that lets a marker-held child dispatch (including
  via `unblock`) while its plan is not `emitted` — except the revision's one
  release: an `unblock` may clear a hold whose plan id is unknown to the
  plans table, evented as `plan_hold_released`.
- Flag any `EMITTING → DISCARDED` path that does not verify the emission
  ledger is empty, and any re-plan of an emitted epic without an explicit
  operator opt-in.
- Flag plan lifecycle state stored anywhere but `plans.status` — never in
  `nodes.state` (ADR-0006 is untouched by planning).

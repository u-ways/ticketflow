# ADR-0007: Dependencies live in the issue body; native links are mirrors

- Status: Accepted
- Date: 2026-08-27
- Revision 2026-08-27: writing native-link mirrors (Jira links, GitHub
  sub-issues) is deferred past M1. The body remains the only read source
  either way; the mirror is cosmetic and can land later without core changes.
- Revision 2026-08-27 (planner, ADR-0014): the grammar gains a third line,
  `tf-plan: <plan_id>/<item_index>` — written only by the planner's emit
  path to tag emitted children, read by sync to hold not-yet-emitted ones.
  A malformed or conflicting marker is reported as an issue like any other
  grammar problem. The pure renderer `render_child_body` is the parser's
  inverse: it raises on anything `parse_body` would report, and emission
  round-trip-checks every rendered body before pushing it, so a malformed
  block can never be emitted. Mirror-writing is now implemented for emitted
  children: Jira `is blocked by` links; GitHub native blocked-by
  relationships with a Projects v2 "Blocked by" field fallback, plus
  sub-issue hierarchy under the epic. Mirrors stay write-only, best-effort,
  and never block emission. `plans.epic_key` stores a tracker key legally:
  `plans` is a planner table, not an edge, lease or scheduling table, and a
  plan must be keyable before its epic is ever synced as a node.

## Context

ticketflow schedules work from a dependency DAG that it reads out of an issue
tracker, and both Jira and GitHub Issues are first-class backends (spec §1).
The two trackers express dependencies through incompatible native mechanisms —
Jira `is blocked by` links versus GitHub sub-issues (spec §7.1) — and both are
freely editable in the tracker UI by anyone with access. If native links were
the source of truth, the canonical graph would differ per backend and a casual
edit in the Jira UI could silently reorder execution.

The domain model (spec §5) therefore needs a single, tracker-agnostic
representation of an edge, anchored to a canonical node identity that no
tracker owns. Relatedly, tickets may carry an optional `scope:` block listing
paths the node expects to touch; spec §12.6 fixes its role as an advisory
efficiency knob for the deterministic scheduler, never a correctness mechanism.

## Decision

Parse dependencies from a `depends-on:` block in the issue body. Treat native
tracker links as write-only mirrors.

- **Grammar.** An edge is declared by a line in the issue body matching
  `depends-on: KEY[, KEY...]` — the keyword is case-insensitive, and each KEY
  is a tracker-native issue key such as `PROJ-41` or `#12`. Both trackers
  store this block identically; the canonical graph is the same regardless of
  backend.
- **Mirrors are write-only.** The tracker adapter writes native Jira links and
  GitHub sub-issues purely for human readability. The core never reads them as
  truth: someone editing links in the tracker UI cannot corrupt the DAG.
  Adapters translate and never decide, per ADR-0002.
- **Canonical identity is ticketflow's.** Each unit of work is a `node` with a
  `node_id` independent of any tracker. The `external_refs` table maps
  `(provider, external_key)` to a `node_id`, so one node may be a Jira ticket
  and a GitHub PR simultaneously. Edges reference `node_id`s, never external
  keys, once resolved.
- **The parser is a pure function.** It takes issue-body text and returns
  edges (or a structured error) with no I/O, and it carries exhaustive unit
  tests. Malformed `depends-on:` blocks are reported — an event in the event
  log (ADR-0005) and a comment on the issue — never guessed at or silently
  dropped.
- **`scope:` is advisory only.** An optional `scope:` block in the issue body
  lists paths the node expects to touch. It is a scheduling bias (overlapping
  ready nodes are preferably staggered, but dispatched anyway rather than
  idling the pool) and prompt context for the worker — never a block
  (spec §12.6). Declared-vs-actual paths are recorded per attempt in the event
  log, which is the signal that decides whether the feature survives. The
  deterministic scheduler (ADR-0008) never puts a model in the loop to
  evaluate a scope hint.

## Consequences

Easier:

- One parser, one grammar, one graph — identical across Jira and GitHub, and
  testable as a pure function against text fixtures.
- The DAG is robust against tracker-UI edits; only an edit to the issue body
  changes the graph, and body edits arrive through the normal sync path.
- Cross-provider nodes (Jira ticket plus GitHub PR) fall out of
  `external_refs` rather than needing a linking convention per tracker.
- The planner (ADR-0014) can materialise a graph by writing plain text into
  ticket bodies, with no per-tracker link API in the emit path.

Harder or deferred:

- The mirrors can drift from the body between syncs; humans reading the
  tracker's native link view may see a stale picture. The body is authoritative
  and the adapter rewrites mirrors, but the lag is real.
- Authors must learn the `depends-on:` convention; native link-editing muscle
  memory does nothing. Malformed-block comments are the teaching mechanism.
- Scope hints add scheduling machinery whose value is unproven. The
  declared-vs-actual record exists precisely so a poorly predicting hint can be
  replaced by a simple parallelism cap later.

## Review guidance

- Flag any code that reads Jira issue links or GitHub sub-issues to construct,
  update, or validate edges — native links may only appear on write paths in
  tracker adapters.
- Flag any edge, lease, or scheduling table that stores a tracker key
  (`PROJ-41`, `#12`) instead of a `node_id`; external keys belong only in
  `external_refs`.
- Require the `depends-on:` parser to remain a pure function (no I/O, no
  adapter or database imports) with unit tests covering case-insensitivity,
  multiple keys, both key styles, and malformed input.
- Flag any parser change that silently ignores or "best-effort" repairs a
  malformed `depends-on:` block; malformed blocks must emit an event-log entry
  and an issue comment.
- Flag any scheduler change that makes a `scope:` overlap a hard block on
  dispatch, or that consults a model to evaluate scope at dispatch time —
  scope is a bias and prompt context only (spec §12.6).
- Require any change to scope-hint handling to preserve the per-attempt
  declared-vs-actual path record in the event log.

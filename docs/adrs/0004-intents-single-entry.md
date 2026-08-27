# ADR-0004: All human signals enter through the intents table

- Status: Accepted
- Date: 2026-08-27

## Context

ticketflow accepts human input from several unrelated surfaces: Jira workflow
transitions, GitHub labels and reviews, the CLI, and a future TUI with buttons
such as retry (spec §4). Each surface has its own vocabulary and its own
temptation to act on state directly — a tracker adapter that flips a node to
`Ready` when a status moves, or a TUI that re-dispatches a node when a button is
pressed, would each be a small, plausible shortcut. Enough of those shortcuts
and the system has two or three competing orchestrators, none of which can be
reasoned about deterministically.

The architecture's invariants forbid this. Invariant 2 states that all human
signals enter through the intents table, whatever the source (spec §2), and the
control-flow model is explicitly "intents in, projections out" (spec §4): one
table on the way in, one append-only event log on the way out (ADR-0005).
Because the orchestrator is deterministic Python driven by a reconcile tick
(ADR-0008) with SQLite as canonical state (ADR-0003), human input must arrive
as durable data the tick can consume — not as calls that mutate state from
outside the loop. Escalated nodes are resolved the same way: a human writes an
intent and the node re-enters the state machine (spec §6, ADR-0006).

## Decision

Every human signal, whatever its source, converges on the `intents` table.

- Support the intent types `approve`, `reject`, `unblock`, `cancel`, `retry`
  and `resume`. A Jira status move, a GitHub label, a CLI command and a future
  TUI button all write the same normalized intent row; the source surface is
  irrelevant to the core.
- No component may mutate node state directly in response to a human action.
  Tracker adapters, the code host adapter, the CLI and the TUI translate human
  actions into intent rows and stop there. The orchestrator consumes pending
  intents during its reconcile tick (ADR-0008) and applies the resulting state
  transitions itself (ADR-0006).
- Store intents as append-only rows with a `processed_at` marker. Never update
  or delete an intent other than to set `processed_at`. Consumption is
  idempotent: re-processing an already-processed intent is a no-op, so a
  crashed tick can safely be re-run.
- Grant the TUI and CLI no privileged path. A TUI retry button writes an intent
  exactly like a tracker status move does; the TUI remains read-only over state
  (spec §11). One path for "a human asked for something" is what stops the TUI
  growing into a second orchestrator.
- Route escalation resolution through intents: a human resolves an `Escalated`
  node by writing an intent, which re-enters the state machine at `Ready`
  (ADR-0006). `resume` after provider quota exhaustion is an intent like any
  other human signal (spec §12.3).
- Route the future planner's plan approval through the same table (ADR-0014).
  Because intents live in SQLite (ADR-0003), an approval gate survives reboots,
  version upgrades, and a human editing the proposal by hand — which is why no
  in-process checkpointer (e.g. LangGraph's `interrupt()`) is used (spec §13.4).

## Consequences

Easier:

- Crash recovery and restarts. Intents are durable rows, so a signal written
  while the orchestrator is down is simply consumed on the next tick; a review
  approval can span days with no resident process (spec §13.3).
- Determinism and testing. The orchestrator's inputs are rows in a table, so a
  tick can be replayed and unit-tested without any live surface attached.
- Adding surfaces. A new front end (web UI, chat bot) only needs to write
  intent rows; the core does not change.
- Auditing. Every human decision is a recorded row with a processed marker,
  complementing the event log's outward record (ADR-0005).

Harder or deferred:

- Latency. A human action takes effect on the next orchestrator tick, not
  immediately; surfaces cannot offer synchronous "it is done" feedback, only
  "it is requested".
- Adapter discipline. Tracker adapters must translate rich, vendor-specific
  actions into six normalized types, and resist handling any of them locally.
- Conflict handling between contradictory intents (e.g. `cancel` and `retry`
  written close together) falls to the orchestrator's consumption order; the
  table itself imposes no policy beyond append order.
- Intent schema evolution is deferred: new human actions require a new intent
  type and orchestrator support, never a new side channel.

## Review guidance

- Flag any code outside the orchestrator's tick/reconcile path that writes to
  node state tables (e.g. `UPDATE`/`INSERT` on `node` state columns) in
  response to a webhook, tracker poll, CLI command or TUI event.
- Flag any adapter, CLI or TUI code that calls orchestrator dispatch,
  transition or scheduling functions directly instead of inserting an intent
  row.
- Flag `UPDATE` or `DELETE` statements against the intents table that touch any
  column other than `processed_at`.
- Flag intent-consumption code that lacks a guard on `processed_at` (processing
  must skip already-processed rows so re-runs are no-ops).
- Flag new intent type strings outside the set {approve, reject, unblock,
  cancel, retry, resume} unless the diff also updates this ADR.
- Require escalation-resolution and quota-resume paths to be implemented as
  intent writes, not as direct state mutation or ad-hoc re-dispatch.
- Require any new human-facing surface (TUI screen, CLI subcommand, web
  endpoint) that accepts an action verb to route it through an intent insert.

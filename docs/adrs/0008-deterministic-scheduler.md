# ADR-0008: Deterministic scheduling: graphlib ready-set, leases, idempotent dispatch

- Status: Accepted
- Date: 2026-08-27

## Context

ticketflow's orchestrator decides what runs next across a dependency graph of
tracker items, with agents working in parallel and the orchestrator itself
liable to crash and restart at any point. The spec is explicit that the
orchestrator is not an agent (spec §3): it is deterministic Python — a
topological ready-set, a lease table, and a reconcile tick — because
reproducibility is what makes crash-recovery, idempotency and unit tests mean
anything. Invariant 4 (spec §2) states the dispatch-side half of this: every
dispatch is idempotent and leased.

Two adjacent problems shape the scheduler. First, parallel nodes can collide on
the same files; the spec delegates conflict serialization to the host's merge
queue and keeps scope hints advisory (spec §12.1, §12.6), so the scheduler must
bias but never block on them. Second, the review loop (spec §9.2) reacts to
external signals — checks, comments, merges — that arrive asynchronously, so
the scheduler needs a single ordered reconcile pass rather than ad-hoc event
handlers, or the loop becomes an expensive oscillator.

## Decision

Scheduling is deterministic: a graphlib ready-set, a lease table (ADR-0003),
and a reconcile tick. No model is ever consulted at dispatch time.

- Use `graphlib.TopologicalSorter` from the standard library to compute the
  dynamic ready-set (`prepare()`/`get_ready()`/`done()`). `prepare()` rejects
  cycles at graph load; a cyclic graph never reaches the scheduler.
- Claim a lease before dispatch, per invariant 4. The orchestrator records the
  lease (worker id, expiry) in SQLite, then dispatches. A lease that expires
  without a heartbeat rolls the node back to `Ready` (ADR-0006). A retried
  dispatch carries the same `(node, attempt)` idempotency key, so it never
  double-spawns an agent process (ADR-0010).
- Run the tick as a fixed, ordered, side-effect-explicit sequence:
  1. consume intents (ADR-0004);
  2. sync tracker;
  3. reconcile running attempts;
  4. settle PRs;
  5. recompute the ready-set;
  6. dispatch.
  Each step is separately unit-testable against fakes; no step performs a side
  effect that is not declared by its position in the sequence.
- Treat scope hints as a dispatch-order bias, never a block. Prefer
  non-overlapping ready nodes when ordering dispatch, but if staggering would
  idle the pool, dispatch anyway (spec §12.6). Scope hints are an efficiency
  knob, and efficiency knobs do not get a model in the loop.
- Delegate merge-conflict serialization to the host merge queue (spec §12.1).
  The scheduler does not build its own serializer. When the queue rejects a PR
  for conflicts, re-dispatch with a narrow conflict-resolution prompt capped at
  one attempt, then escalate (ADR-0006).

## Consequences

- Crash-recovery becomes a matter of re-reading SQLite: the ready-set is
  recomputed from persisted graph state, leases identify what was in flight,
  and the idempotency key makes re-dispatch after an uncertain crash safe.
  Combined with adoption on restart (ADR-0010), the orchestrator can die at any
  point in the tick without losing or duplicating agent work.
- Every scheduling decision is reproducible and unit-testable: the same
  database state and the same external answers produce the same tick, and each
  tick step tests in isolation against fakes for the tracker, runner and code
  host ports (ADR-0002).
- The tick is polling-shaped. Reaction latency is bounded by tick cadence
  rather than webhook arrival, which is accepted: the settle window (spec §9.2)
  wants batching anyway, and a per-event push path would reintroduce the
  oscillator the spec warns against.
- Scope-hint staggering is best-effort, so overlapping nodes will sometimes run
  concurrently and burn CI on rebases. That cost is accepted because the merge
  queue guarantees correctness; declared-vs-actual scope is recorded so the
  hint can later be replaced by a simple parallelism cap if it predicts poorly.
- Smarter scheduling — model-assisted prioritisation, dynamic re-planning — is
  deliberately foreclosed. An LLM may generate `scope:` blocks offline at
  planning time (ADR-0014), but nothing model-driven enters the dispatch path.
- The one-attempt cap on conflict resolution trades convergence for safety:
  some resolvable conflicts will escalate to a human, which is preferred to an
  agent silently discarding other people's work.

## Review guidance

- Flag any model or LLM client invocation in `orchestrator`, `graph`, or
  `store` — no model runs in the scheduling loop. The dispatch step may call
  `RunnerPort.start`, but nothing in the tick may consult a model. (Canonical
  rule: ADR-0002 and ADR-0014 defer to this bullet.)
- Flag any topological-sort or cycle-detection implementation other than
  `graphlib.TopologicalSorter`, and any graph load path that does not call
  `prepare()` before scheduling.
- Require every dispatch call site to be preceded by a lease claim recording
  worker id and expiry, and to carry a `(node, attempt)` idempotency key.
- Require lease-expiry handling to transition the node to `Ready`, not to
  re-dispatch directly or mark the node failed.
- Flag reordering, merging, or conditional skipping of the tick steps
  (intents → tracker sync → reconcile → settle → ready-set → dispatch), and
  any new tick step that lacks its own unit test against fakes.
- Flag any code path where a scope hint blocks dispatch (rather than biasing
  order) or where staggering is applied while the worker pool would idle.
- Flag any scheduler-side merge-conflict serialization (custom rebase queues,
  cross-PR locks) — conflict ordering belongs to the host merge queue.
- Require conflict-resolution redispatch to be capped at one attempt with
  escalation on failure, distinct from the ordinary feedback cycle cap.

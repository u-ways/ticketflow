# ADR-0006: Node lifecycle state machine with a single escalation state

- Status: Accepted
- Date: 2026-08-27
- Revision 2026-08-27: three edges added to the table beyond the spec's
  diagram — `InProgress → Merged` (bootstrap: no repo existed, so the initial
  push completes the node, spec §9.1 step 0), and `Blocked → Escalated` /
  `Ready → Escalated` (an operator cancel/reject intent pulls a node that has
  never run out of the graph; escalation already being the single
  needs-a-human state, it also records deliberate withdrawal). The table
  carries each edge's guard as a named condition; the guard's *predicate* is
  enforced at the edge's single orchestrator call site, because evaluating it
  needs I/O context (PR status, the lease table, run dirs) that the domain
  layer must not import. The store still rejects any edge not in the table.

## Context

Every node moves through a lifecycle driven by deterministic events: dependencies
resolving, a lease being claimed, a runner exiting, CI and review signals
arriving (spec §6). The orchestrator is not an agent — it is deterministic
Python (spec §3) — so the lifecycle must be explicit and enumerable, with no
judgement calls hidden in scheduling code.

Some failures cannot be fixed by iteration. Runner crashes, timeouts and empty
diffs never produce a PR, so the PR review loop cannot catch them; without an
explicit edge they fall through to lease expiry and retry silently forever,
which is the expensive failure mode (spec §6). The machine therefore needs a
deliberate needs-a-human state, and a rule for what happens to the dependents of
a node stuck in it (spec §12.4).

## Decision

Adopt the spec's node state machine (spec §6) exactly.

- The states are `Blocked`, `Ready`, `InProgress`, `AwaitingSignals`,
  `AddressingFeedback`, `Merged`, `Escalated`. `Merged` and `Escalated` are
  terminal for the orchestrator.
- Guard every transition:
  - `Blocked → Ready` fires only when all upstream edges are resolved. This is
    the only place graph structure matters.
  - `Ready → InProgress` fires only after the orchestrator claims a lease,
    before dispatch (ADR-0008). A lease that expires without a heartbeat rolls
    the node back from `InProgress` to `Ready`.
  - `AwaitingSignals → Merged` is the composite condition: checks green, plus
    required approvals, plus all review threads resolved. All three parts are
    required; none alone is terminal.
- `Escalated` is the single needs-a-human state, reachable from any active
  state. Implement the spec's trigger table:

  | From | Trigger |
  |---|---|
  | InProgress | Runner crashed, repeated across attempts |
  | InProgress | Wall-clock timeout |
  | InProgress | Clean exit with an empty diff |
  | InProgress | Repeated lease expiry |
  | InProgress | Tool policy denial |
  | InProgress | Provider quota exhaustion |
  | AwaitingSignals | Checks stuck red past the cycle cap |
  | AddressingFeedback | Feedback cycle cap exceeded |
  | Blocked, Ready | Operator cancel/reject intent (revision above) |

- Two further legal edges beyond the trigger table: `InProgress → Merged` for
  the bootstrap case (the node's work is its initial push, spec §9.1 step 0),
  and `InProgress → AwaitingSignals`'s counterpart `InProgress → Ready` on
  lease expiry.

- Escalation is terminal for the orchestrator. A human resolves it by writing an
  intent (ADR-0004), which re-enters the machine at `Ready` with attempt
  counters reset. The spec attaches the counter reset only to the
  feedback-to-the-agent exit (spec §6); this ADR deliberately sharpens it to
  every human re-entry, since a human chose to re-dispatch. Exactly three
  documented exits: feedback to the agent, fix it directly, or fix the ticket.
- Dependents of an `Escalated` node stay `Blocked`, with a `blocked_reason`
  naming the escalated ancestor so the board shows root cause rather than a
  silent stall (spec §12.4). Never cancel the subtree — that destroys work and
  the information about why. Never let dependents proceed — they would build on
  a foundation nobody approved.
- Apply the halt heuristic: when the scheduler finds nothing dispatchable for N
  ticks while escalations exist, halt and notify. A process that looks healthy
  and is doing nothing is worse than one that stops.
- Implement all transitions in one module as an explicit table of
  `(from_state, to_state, guard)` entries. An illegal transition raises; no code
  path mutates a node's state except through this table.

## Consequences

- Easier: crash recovery and testing. A closed transition table makes every
  lifecycle path enumerable, so reconciliation after restart (ADR-0010) and unit
  tests can assert exhaustively over states rather than over scattered flag
  checks. State changes recorded against the table also project cleanly onto the
  event log (ADR-0005).
- Easier: diagnosing stalls. A single `Escalated` state with a named trigger,
  and dependents carrying `blocked_reason`, means the board answers "why is
  nothing happening" without log archaeology.
- Harder: adding a new lifecycle stage means changing the table, its guards and
  the trigger inventory in one reviewed place. That friction is intended.
- Harder: one escalated node stalls its whole subtree until a human acts. This
  is accepted — the graph waiting on one human decision beats building on
  unapproved work.
- Deferred: the value of N in the halt heuristic, along with the cycle caps that
  feed two of the escalation triggers, are tuning values to be set from real
  runs (spec §16). Automated recovery from escalation is deliberately not
  provided; the only exit is a human intent.

## Review guidance

- Require that every node state transition goes through the single transition
  table module; flag any code that assigns a node state directly outside it.
- Flag any addition or removal of a node state, or any new `(from, to)` pair,
  that is not accompanied by an update to this ADR.
- Flag any transition into `InProgress` that is not preceded by a successful
  lease claim, and any lease-expiry path that does not return the node to
  `Ready` or escalate on repetition.
- Flag any `AwaitingSignals → Merged` code path gated on fewer than all three
  conditions: checks green, approvals satisfied, review threads resolved.
- Flag any second human-attention state (e.g. `NeedsReview`, `Paused`,
  `Stuck`): escalation must remain the single needs-a-human state.
- Flag any code that cancels, re-dispatches or unblocks dependents of an
  `Escalated` node; dependents must remain `Blocked` with a `blocked_reason`
  naming the escalated ancestor.
- Require that automated re-entry from `Escalated` is impossible: the only exit
  is a human-written intent that re-enters at `Ready` and resets attempt
  counters.
- Flag removal or weakening of the illegal-transition raise, or of the halt
  heuristic that stops the scheduler when nothing is dispatchable while
  escalations exist.

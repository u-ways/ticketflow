# ADR-0005: An append-only event log drives every outward projection

- Status: Accepted
- Date: 2026-08-27

## Context

The control flow is deliberately asymmetric: every human signal converges on the
intents table (ADR-0004), and every outward surface — tracker boards, the TUI,
telemetry — is fed from a single append-only event log (spec §4). SQLite holds
canonical state (ADR-0003); the surfaces people look at are projections of it
and may lag. Without one log, each surface would grow its own read path into
core state, and the TUI in particular would drift towards becoming a second
orchestrator.

Observability sharpens the requirement. Holding OTel spans open in memory across
a two-hour node attempt means a crash loses the trace, so the spec directs that
spans be emitted from recorded timestamps in the event table by a tailer, not
from live control flow (spec §11). The retention policy completes the picture:
agent logs are swept after 14 days, but the event log is the one class that is
never deleted (spec §12.7), which is also what makes after-the-fact observations
such as "we merged 40 PRs with no gates" possible (spec §8.4).

## Decision

Maintain a single append-only `events` table in SQLite as the source for every
outward surface.

The orchestrator writes an event row for every observable fact it produces or
consumes:

- every dispatch and every node state transition (ADR-0006);
- every check observation and every merge;
- every escalation;
- every log truncation or eviction — history that vanishes silently is worse
  than history that is gone;
- every cost record, normalized at the runner adapter boundary;
- every run-level fact, such as the `--yolo` flag (ADR-0013).

Projections read the log and only the log:

- the tracker board projection written back through the tracker adapters;
- any TUI view, which stays read-only over state (ADR-0004 governs its writes);
- a future OTel tailer that reads the table and emits spans from recorded
  timestamps. Telemetry is thereby a projection: it can lag, fail, or be
  replayed without touching correctness. The tailer itself is deferred, but the
  design is accepted now, so every event carries its timestamp and correlation
  ids (node_id, attempt, trace id where present) from day one.

Never delete event rows. The event log is the "never deleted" class in the
retention table (spec §12.7); no sweep, age limit, or size cap applies to it.

Keep event rows as plain columns — `id`, `ts`, `node_id`, `attempt`, `kind`,
`payload` (JSON) — written with plain `INSERT` statements. Do not adopt an
aggregate or event-sourcing framework; the spec explicitly rejects the
`eventsourcing` package because it imposes an aggregate model on what is one
table (spec §15.2).

Correctness must never depend on a projection. Deleting every projection —
boards, TUI state, emitted spans — must lose nothing: canonical state remains in
SQLite and every projection is re-derivable from the log.

## Consequences

Easier:

- Crash-safe telemetry. A tailer replaying recorded timestamps survives
  orchestrator restarts; an in-memory span would not.
- Auditability for free. Which checks reported on each PR and how each merge
  happened are recorded as observation, not validation, and stay queryable
  forever.
- New surfaces are cheap. A web UI, a metrics exporter, or a replacement TUI is
  another reader of the same table, with no new write path into core state.
- Testing. Projections are pure functions of an inspectable log.

Harder or deferred:

- The OTel tailer is deferred, so there are no spans until it is built; the
  cost now is the discipline of stamping timestamps and correlation ids on
  every event.
- The table grows without bound by design. Rows are kilobytes against
  megabytes of agent logs, so this is accepted, but the schema cannot rely on
  pruning to fix a bad payload decision.
- `payload` is unversioned JSON, so consumers must tolerate unknown kinds and
  absent fields rather than assuming a closed schema.

## Review guidance

- Flag any `UPDATE` or `DELETE` statement targeting the events table, in code
  or in migrations; event rows are insert-only and permanent.
- Flag any retention, sweep, or cap-eviction code path that includes the events
  table; only agent logs and other artifact classes are subject to retention.
- Require every event insert to populate `ts`, `kind`, and — where the event
  concerns a node — `node_id` and `attempt`, so the deferred OTel tailer can
  emit spans and links from recorded data alone.
- Flag orchestrator code that performs a dispatch, state transition, check
  observation, merge, escalation, truncation/eviction, or cost record without a
  corresponding event insert in the same unit of work.
- Flag projection code (tracker board writers, TUI views, any tailer) that
  writes to canonical state tables or is read by scheduling logic; projections
  read the log and must be deletable without loss.
- Flag any new dependency on an event-sourcing or aggregate framework (e.g.
  `eventsourcing`) in `pyproject.toml`; the log is one plain table.
- Flag OTel span construction driven by live control flow (spans opened before
  the work and closed after it) rather than emitted from recorded event
  timestamps.

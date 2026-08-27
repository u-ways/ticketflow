# ADR-0003: SQLite is the canonical state store

- Status: Accepted
- Revision 2026-08-27: three tables beyond the spec's domain model —
  `handoffs` (mandated by ADR-0013), `check_stats` (per-check flake rates,
  ADR-0009), and `kv`, a small bookkeeping table for orchestrator operational
  state: sync/projection cursors, the dispatch-pause flag, per-node PR
  numbers, bootstrap flags, pending operator feedback, unresolved-dependency
  holds, and per-cycle check re-run records. `kv` never carries canonical
  node state (states, edges, leases, attempts live in their own tables); its
  values are operational marks that are either reconstructible or advisory.
  One narrow write exception accompanies the reader rule: intent ingress
  (ADR-0004). CLI/TUI commands may append rows to the **intents table only**
  — that is precisely how a human signal enters — serialized against the
  single orchestrator writer by WAL and busy_timeout. Status views open
  read-only connections and every other table stays orchestrator-only.
- Revision 2026-08-27 (planner, ADR-0014): migration 4 adds the planner
  tables — `plans` (lifecycle rows, one live plan per epic via a partial
  unique index), `plan_revisions` (append-only byte-exact YAML blobs), and
  `plan_emitted_items` (the emission ledger whose primary key is the
  idempotency key). A second, narrowly scoped writer joins the intent-ingress
  exception: a planner CLI turn (`ticketflow plan ...`) may write the
  `plan*` tables, set `processed_at` on `plan_*` intents it consumes
  (ADR-0004), and append events. It never writes nodes, edges, leases,
  attempts or kv; the concurrency with a running orchestrator is brief,
  touches disjoint tables, and is serialized by WAL and busy_timeout. The
  orchestrator reads `plans.status` (for the emission hold, ADR-0014) and
  never writes plan tables.
- Date: 2026-08-27

## Context

The first invariant of the architecture is "SQLite is truth" (spec §2): boards,
traces and the TUI are projections of orchestrator state and may lag. For that
invariant to mean anything, exactly one store must hold canonical state, exactly
one process must write to it, and everything else must be derivable from it.
ticketflow is a single-operator, local process that must survive its own restart
without losing in-flight agent work, so the store must also be durable, embedded
and dependency-free.

The dependency review (spec §15.1) selects stdlib `sqlite3` — WAL mode with
`busy_timeout`, and a lease pattern modelled on `litequeue` — precisely because
it needs no server, no ORM and no external service. The observability section
(spec §11) already assumes this shape: the TUI is read-only, opens its own
connection under WAL, and never holds a transaction across a render, with
`trace_id` stored on the SQLite row so the TUI can deep-link into traces. This
ADR makes those constraints normative.

## Decision

Use SQLite, via the standard library `sqlite3` module, as the single canonical
state store. No ORM and no migration framework are introduced.

- **One database file.** Open it in WAL mode with `busy_timeout` set. The
  orchestrator process is the only writer; no adapter, TUI, or helper process
  ever writes to the database directly.
- **Everything else is a projection.** Tracker boards, OTel traces and any TUI
  render from orchestrator state and may lag behind it (invariant 1, spec §2).
  A projection that disagrees with the database is stale, not authoritative.
- **The schema comprises these tables:** `nodes`, `external_refs` (`node_id`,
  `provider`, `external_key`, `etag`), `edges`, `attempts`, `leases`, `intents`
  and `events`, plus `handoffs`, `check_stats` and the `kv` bookkeeping table
  (revision above). Leases carry a worker id and an expiry, following the
  litequeue-modelled claim pattern: a worker claims a lease before dispatch and
  an expired lease releases the node.
- **Migrate with plain sequential SQL scripts**, applied in order and tracked
  via the `user_version` pragma. Do not add an ORM, a schema DSL, or a
  migration framework.
- **Readers open read-only connections.** Status commands and any future TUI
  connect read-only and never hold a transaction open across a render (spec
  §11). Reads must never block the writer beyond what WAL already implies.
- **No state lives only in process memory.** A restart, the `runs/` directory
  and this database must together be sufficient to resume every in-flight node
  (see ADR-0010). Anything the orchestrator would need after a crash must be
  written to the database or the run directory before it is relied upon.

## Consequences

Easier:

- Crash recovery and restart adoption (ADR-0010) reduce to reading one file
  plus `runs/`; there is no in-memory session to reconstruct.
- Tests run against a throwaway database file or `:memory:`; no service to
  provision, no fixtures beyond SQL.
- The intents table (ADR-0004) and event log (ADR-0005) inherit the same
  durability and the same single-writer discipline for free.
- Deployment stays a single local process with zero external state services,
  and stdlib-only storage adds nothing to the dependency pin list.

Harder or deferred:

- The single-writer rule means all mutations funnel through the orchestrator;
  any future component that wants to change state must send an intent rather
  than write a row, which adds a hop but is the point.
- Hand-written sequential SQL migrations demand more discipline than a
  framework: ordering mistakes and forgotten `user_version` bumps are caught by
  review, not tooling.
- Multi-host or multi-writer operation is out of scope; scaling beyond one
  local process would require revisiting this ADR, and that is accepted.
- Projections lagging is by design, so surfaces must tolerate stale reads
  rather than demanding read-your-writes freshness.

## Review guidance

- Flag any dependency on an ORM or migration framework (e.g. SQLAlchemy,
  Alembic, peewee, Django ORM) added to project dependencies or imports; state
  access is stdlib `sqlite3` only.
- Flag any database write (`INSERT`, `UPDATE`, `DELETE`, DDL) issued outside
  the orchestrator process's code paths — in particular from adapter, TUI, or
  status-command modules — except the two scoped exceptions of the revisions
  above: intent ingress, and planner turns writing `plan*` tables, `plan_*`
  intent `processed_at`, and events. Flag planner code writing nodes, edges,
  leases, attempts or kv.
- Require reader code paths (status commands, TUI) to open connections
  read-only (e.g. `mode=ro` URI or equivalent) and flag any reader that begins
  a transaction spanning a render or holds a cursor across UI drawing calls.
- Require new schema changes to arrive as a new sequential SQL script that
  bumps `user_version`; flag schema statements executed ad hoc from application
  code or scripts that modify an already-merged migration.
- Flag connection setup that omits WAL mode or `busy_timeout`, and flag any
  second writer connection opened alongside the orchestrator's other than a
  planner CLI turn within its scoped table set (revision above).
- Flag new orchestrator state held only in process memory — long-lived dicts,
  caches or queues tracking node, lease, attempt or intent state that is not
  backed by a table or the `runs/` directory (see ADR-0010).
- Flag any code that treats a projection (board state, trace data, TUI view) as
  authoritative input for scheduling decisions instead of reading the database.

# ADR-0002: Ports-and-adapters core with three vendor ports

- Status: Accepted
- Date: 2026-08-27
- Revision 2026-08-27: CodeHostPort widened beyond the spec's five methods.
  `repo_exists()`/`default_branch()`/`branch_exists(branch)` serve workspace
  detection and push verification (ADR-0010); `find_pr_for_branch(branch)`
  keeps PR opening idempotent; `enable_auto_merge(pr)`,
  `rerun_failed_checks(pr)` and `post_comment(pr, text)` serve the merge
  ladder, flake handling and handoff comments (ADR-0009, ADR-0013). All are
  reads of, or writes to, branches and PRs only — invariant 5 is untouched.
  TrackerPort's write methods take the tracker-native `external_key`
  (`push_state(external_key, state)`, `push_comment(external_key, text)`):
  the caller resolves canonical `node_id` → key via `external_refs` before
  crossing the boundary, because the alternative — adapters resolving it —
  would force adapters to read the store, which this ADR forbids. The
  canonical graph still never stores vendor keys (ADR-0007).
- Revision 2026-08-27 (planner, ADR-0014): TrackerPort widened with exactly
  three methods for plan emission —
  `create_item(title, body, labels=(), parent_key=None) -> external_key`,
  `update_body(external_key, body)`, and
  `mirror_dependencies(external_key, depends_on)` (the write-only native
  mirror of ADR-0007; `parent_key` is the cosmetic hierarchy mirror). All
  take or return tracker-native keys per the revision above; adapters still
  translate and never decide — emission idempotency lives with the caller.
  The package layout gains `src/ticketflow/planner/` (the offline phase; no
  vendor SDK and no model client inside it). pydantic-ai is treated as
  model-API vendor surface: its import is confined to
  `src/ticketflow/adapters/pydanticai_synthesis.py`, which implements the
  planner-internal `PlanSynthesizer` Protocol — a seam inside the planner
  package, NOT a fourth port; the port count stays exactly three. The
  no-model rule for `orchestrator`/`graph`/`store` remains ADR-0008's
  canonical bullet.

## Context

ticketflow must be tracker-agnostic (Jira and GitHub Issues are both first-class)
and runner-agnostic (Claude Code and GitHub Copilot CLI are both first-class),
while GitHub serves as the code host (spec §1, §3). The backends are unequal:
Jira has native `is blocked by` links and workflow statuses where GitHub Issues
has sub-issues, Projects v2 fields, or plain labels (spec §7.1). A core that
assumed any one vendor's capabilities would either underuse the richer backends
or break on the poorer ones.

The scheduling core must also stay deterministic and unit-testable — a
topological ready-set, a lease table, and a reconcile tick (spec §3) — which is
only possible if vendor I/O sits behind interfaces the tests can replace, and if
no vendor-specific behaviour leaks into scheduling decisions (invariant 3,
spec §2). Finally, teams commonly plan in Jira but host code on GitHub, so
"tracking" and "code hosting" cannot be a single vendor-shaped seam.

## Decision

Structure the core as a hexagon with exactly three ports. The signatures below
are normative.

- **TrackerPort**: `fetch_nodes(cursor)`, `fetch_intents(cursor)`,
  `push_state(external_key, state)`, `push_comment(external_key, text)`,
  `create_item(title, body, labels=(), parent_key=None)`,
  `update_body(external_key, body)`,
  `mirror_dependencies(external_key, depends_on)`,
  `capabilities()` (key-based per the revisions above; the last three exist
  for plan emission, ADR-0014).
- **RunnerPort**: `start(node, workspace, policy)`, `poll(handle)`,
  `resume(handle, feedback)`, `cancel(handle)`, `capabilities()`.
- **CodeHostPort**: `open_pr(branch, title, body)`, `get_pr_status(pr)`,
  `get_feedback(pr, since)`, `resolve_thread(thread_id)`, `merge(pr)`, plus
  (revision above) `repo_exists()`, `default_branch()`,
  `branch_exists(branch)`, `find_pr_for_branch(branch)`,
  `enable_auto_merge(pr)`, `rerun_failed_checks(pr)`,
  `post_comment(pr, text)`.

Each port exposes `capabilities()`, and the core asks rather than assumes:
unequal backends declare what they support (for example
`native_dependency_links`, `custom_state_field`), and the core degrades
accordingly instead of hard-coding a vendor's feature set.

The core never learns a vendor's vocabulary. No vendor SDK type crosses a port
boundary; adapters translate to canonical domain types at their own edge.
Adapters translate and never decide: no scheduling logic lives in an adapter
(invariant 3).

GitHub gets two independent adapters — a tracker adapter (Issues/Projects) and
a code host adapter (PRs/checks). Keeping them separate is what allows
Jira-for-planning plus GitHub-for-code.

The orchestrator is deterministic Python. No model runs in the scheduling loop
(the scheduling mechanics are ADR-0008). Agents run inside nodes; they never
decide what runs next.

Lay the packages out as
`src/ticketflow/{domain,store,graph,orchestrator,planner,ports,supervision,cli,adapters/{github_tracker,jira_tracker,github_codehost,claude_runner,pydanticai_synthesis}}`.
Tests exercise the core through in-memory fakes — `FakeTracker`, `FakeRunner`,
`FakeCodeHost` — implementing the same port interfaces the real adapters do.

## Consequences

- Swapping or adding a vendor is an adapter, not a core change: a new tracker,
  runner, or code host implements an existing port and touches nothing else.
  Fast-moving SDKs (both agent SDKs move weekly) are pinned and isolated so an
  upgrade touches one file.
- The core is testable without network access: the fakes drive the scheduler,
  state store (ADR-0003), and node lifecycle (ADR-0006) through the same
  interfaces production uses.
- Mixed deployments — Jira for planning, GitHub for code — fall out of the
  two-adapter split rather than needing special support.
- Every vendor concept must be translated into canonical domain types, which is
  ongoing work: each new backend feature means extending the canonical model or
  a `capabilities()` flag, and the flag set can grow untidily if not curated.
- The async-shaped RunnerPort is deliberately more general than today's local
  processes need, so that a remote agent (assign-issue-and-wait) fits later
  without reshaping the core (spec §7.2); runner process mechanics are
  ADR-0011.
- CodeHostPort has one production adapter (GitHub) for now. Other code hosts
  are deferred; the port exists today so `FakeCodeHost` can run in tests.

## Review guidance

- Flag any import of a vendor SDK (`githubkit`, `atlassian-python-api`,
  `claude-agent-sdk`, `github-copilot-sdk`) outside `src/ticketflow/adapters/`,
  and any `pydantic_ai` import outside `src/ticketflow/adapters/` (revision
  above).
- Flag any vendor SDK type appearing in a port method signature under
  `src/ticketflow/ports/` or in `src/ticketflow/domain/`.
- Flag any change to the method set or signatures of TrackerPort, RunnerPort,
  or CodeHostPort that does not also update this ADR.
- Flag adapter code (`src/ticketflow/adapters/**`) that imports from
  `orchestrator`, `graph`, or `store`, or that touches leases, the ready-set,
  or state-transition decisions.
- Flag a new port module under `src/ticketflow/ports/` — the count is exactly
  three unless an ADR supersedes this one.
- Require core and orchestrator tests to use `FakeTracker`, `FakeRunner`, and
  `FakeCodeHost` through the port interfaces rather than mocking vendor SDKs.
- Require a new backend integration to arrive as a new package under
  `src/ticketflow/adapters/` implementing an existing port, with no core edits
  beyond registration.

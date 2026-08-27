# Architecture Decision Records

The normative rulebook for ticketflow. `docs/architecture.md` is background
context; **where it and an ADR disagree, the ADR wins** (ADR-0001).

Every PR is reviewed against these files by the automated ADR reviewer
(`.github/workflows/adr-review.yml`). The `## Review guidance` section of each
ADR is the reviewer's rubric; its comments block merge until resolved.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions and enforce them in review | Accepted |
| [0002](0002-ports-and-adapters-core.md) | Ports-and-adapters core with three vendor ports | Accepted |
| [0003](0003-sqlite-canonical-state.md) | SQLite is the canonical state store | Accepted |
| [0004](0004-intents-single-entry.md) | All human signals enter through the intents table | Accepted |
| [0005](0005-event-log-projections.md) | An append-only event log drives every outward projection | Accepted |
| [0006](0006-node-state-machine.md) | Node lifecycle state machine with a single escalation state | Accepted |
| [0007](0007-dependencies-in-issue-body.md) | Dependencies live in the issue body; native links are mirrors | Accepted |
| [0008](0008-deterministic-scheduler.md) | Deterministic scheduling: graphlib ready-set, leases, idempotent dispatch | Accepted |
| [0009](0009-repo-owns-quality-gates.md) | ticketflow implements no quality gates; the target repo owns them | Accepted |
| [0010](0010-detached-process-supervision.md) | Detached-process supervision with adoption on restart | Accepted |
| [0011](0011-runner-adapters-headless-cli.md) | Runner adapters: headless CLI first, SDK client attach later | Accepted |
| [0012](0012-toolchain-and-delivery.md) | Toolchain and delivery: Python 3.14, uv, just, TDD, CI gates | Accepted |
| [0013](0013-operational-policies.md) | Operational policies: yolo mode, spend, retention | Accepted |
| [0014](0014-planner.md) | The planner is a separate offline phase | Accepted |

## Format

MADR-lite: `NNNN-slug.md` with exactly these sections — title, Status/Date,
Context, Decision, Consequences, Review guidance. Statuses: Proposed, Accepted,
Superseded (with pointer), Deprecated. Architecturally significant changes land
in the same PR as the ADR that permits them.

# ADR-0001: Record architecture decisions and enforce them in review

- Status: Accepted
- Date: 2026-08-27

## Context

ticketflow's design rests on a small set of invariants that hold everywhere
(spec §2): SQLite is the canonical state, all human signals enter through the
intents table, adapters translate but never decide, every dispatch is idempotent
and leased, and the target repo is read-only except for branches and PRs. The
spec also fixes the shape of the system — three vendor ports (spec §7), a
deterministic node state machine (spec §6), and a deliberately pinned dependency
stack (spec §15). These constraints are easy to state and easy to erode: a
convenient shortcut in an adapter or a new transitive dependency can violate
them without any single reviewer noticing.

The project is developed test-first (ADR-0012) and much of the implementation
work will be done by coding agents. Agents follow written rules well and
unwritten rules not at all, so the architecture must live in the repository as
reviewable text, and conformance must be checked mechanically on every pull
request rather than depending on a human remembering the spec.

## Decision

Record every architecture decision as a lightweight MADR-style ADR in
`docs/adrs/NNNN-slug.md`, numbered sequentially. See the full index starting at
ADR-0002 through ADR-0014.

- **Every architecturally significant change lands in the same PR as the ADR
  that permits it** — either a new ADR or an amendment to an existing one
  carrying an explicit revision note. "Architecturally significant" means
  anything touching: the spec invariants (SQLite is truth, ADR-0003;
  intents-only human signals, ADR-0004; adapters translate and never decide,
  ADR-0002; idempotent leased dispatch, ADR-0008; the target repo read-only
  except branches and PRs, ADR-0009); the port interfaces (ADR-0002); the node
  state machine (ADR-0006); or the introduction of a new external dependency
  (ADR-0012).
- **An ADR carries one of four statuses**: Proposed, Accepted, Superseded (with
  a pointer to the superseding ADR), or Deprecated.
- **Enforcement is automated.** `.github/workflows/adr-review.yml` runs a
  Claude Opus 5 review agent — the headless Claude Code CLI, model
  `claude-opus-5` — on every pull request. The agent reads `docs/adrs/`,
  reviews the diff for drift from the accepted ADRs, posts inline PR review
  comments for each violation, and fails its status check while violations
  exist. Branch protection makes that check required and blocks merge on
  unresolved conversations; the branch-protection configuration itself is
  owned by ADR-0012.
- **Every ADR ends with a "Review guidance" section** listing the concrete
  rules the CI reviewer applies to a diff. The ADRs are the reviewer's only
  rubric: vague ADRs make a noisy reviewer, so guidance must be mechanically
  checkable against a pull request diff.
- **`docs/architecture.md` (the imported solution-architecture document) is
  context, not rubric.** Where it conflicts with an ADR, the ADR wins, and the
  conflict must be fixed in the same PR that surfaces it.

## Consequences

- The architecture becomes self-enforcing: an agent or human proposing a change
  that breaches an invariant is blocked by a required check, not by tribal
  knowledge. New contributors — human or agent — get the complete normative
  rulebook by reading one directory.
- Writing an ADR becomes a cost on every significant change. This is accepted:
  the same-PR rule keeps decision and implementation reviewable together, and
  it prevents the codebase drifting ahead of its documentation.
- Review quality is bounded by ADR quality. A poorly specified "Review
  guidance" section produces false positives that erode trust in the check, so
  authors carry the burden of writing mechanically checkable rules.
- The merge path takes a hard dependency on an external model and the
  `adr-review.yml` workflow. An outage or model deprecation blocks merges until
  the workflow is fixed or an administrator intervenes.
- Superseded and Deprecated ADRs remain in the directory, so the reviewer must
  weigh status when applying rules; history is preserved at the cost of a
  larger rubric.
- Retro-fitting decisions not yet captured as ADRs is deferred: until a topic
  has an ADR, the reviewer has nothing to enforce on it.

## Review guidance

- Require every PR that modifies port interfaces, the node state machine,
  lease or dispatch logic, the intents table, or the SQLite schema to also add
  or amend a file under `docs/adrs/` in the same PR; flag such diffs when no
  ADR change is present.
- Require every new or renamed ADR file to match `docs/adrs/NNNN-slug.md`, to
  contain a Status line with exactly one of Proposed, Accepted, Superseded, or
  Deprecated, and to end with a "Review guidance" section of Flag/Require
  bullets.
- Flag any ADR whose status becomes Superseded without a pointer to the
  superseding ADR, and any amendment to an Accepted ADR that lacks a revision
  note.
- Flag any PR adding a new external dependency (a new entry in the project's
  dependency manifest or lockfile) without a corresponding ADR addition or
  amendment in the same diff.
- Flag any change to `docs/architecture.md` that contradicts an Accepted ADR;
  the ADR must be amended in the same PR or the change rejected.
- Flag any edit to `.github/workflows/adr-review.yml` that removes the ADR
  review step, changes the model from `claude-opus-5`, or stops the check
  failing on violations, unless an ADR amendment in the same PR permits it.

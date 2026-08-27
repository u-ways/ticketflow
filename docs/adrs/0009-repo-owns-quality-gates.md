# ADR-0009: ticketflow implements no quality gates; the target repo owns them

- Status: Accepted
- Date: 2026-08-27

## Context

The governing bet of the architecture (spec §1.1) is that a repo safe to hand to
an autonomous agent is a repo that was already well run. Branch protection,
required checks, real tests and reviewers who catch things are the safety
system; ticketflow adds none of its own and inherits the host's. The dominant
practical failure of "make CI green" as a goal is that the agent makes CI green
by weakening CI — deleting the failing test, loosening an assertion, adding
`# noqa`, suppressing a scanner finding, marking a test flaky (spec §8).
Detecting that well is repo-specific, language-specific and high-false-positive:
it belongs next to the code, in the repo's own pipeline, owned by the team who
can judge whether a given weakening was legitimate.

Checks that live in a framework are checks the team does not own, cannot tune,
and will not maintain — and they would create the impression that installing a
tool made a repo safe. The repo makes the repo safe. This ADR fixes that
boundary so it cannot erode one convenience feature at a time.

## Decision

Implement no quality or integrity gates in ticketflow. Consume the host's merge
answer instead.

- Hold no configuration describing the repo's checks: no check names, no check
  classification, no preflight, no conformance scan, no diff parsing, and no
  opinion about the code (spec §8.3, §8.4). Ask the host one question — can
  this pull request be merged? — and act on the answer.
- Treat the target repo as read-only except for branches and PRs (invariant 5):
  never configure gates, never modify workflows, never judge a diff.
- Answer the merge decision on every settle by walking this ladder, in order
  (spec §9.1):
  0. The repo does not exist yet — there is no PR; the node is done when its
     initial push succeeds.
  1. Any check is red — the agent fixes it; loop.
  2. Review threads are unresolved — the agent addresses them; loop.
  3. Required approvals are satisfied — merge.
  4. Otherwise, set auto-merge if the host allows it; the host merges when the
     last approval lands.
  5. No gates apply at all — merge.
- Run the loop with a settle window: wait until all checks have reported and
  review agents have posted, then dispatch once with the batched feedback.
  Never re-dispatch per comment.
- Cap review cycles at a configurable default of 100, tracked separately from
  dispatch attempts. A cap that fires signals the ticket is wrong, not that the
  agent needs one more try.
- Re-run a failed check once before treating it as the agent's problem, and
  record per-check flake rates in SQLite — an agent handed a flaky signal will
  rationally "fix" it by deleting the test.
- Keep local gates to a small, fast pre-push smoke check so obviously broken
  pushes do not burn CI.
- Publish guidance instead of enforcing it: `REPO_REQUIREMENTS.md` carries the
  thesis, recommendations R1–R6, and the weakening-detection techniques
  (coverage ratchet, suppression scan, test-count/skip ratchet, baseline-diffed
  static analysis, a policy reviewer on a different model or provider to the
  worker). Ship the corresponding reference workflow templates in `examples/`
  as illustrative and unsupported — the repo owns them.
- Advise that gates apply to every PR, human or agent, with no difference
  (spec §12.5). A gate that only applies to one author class teaches people
  which author class to use.
- Record, via the event log (ADR-0005), which checks reported on each PR and
  how each merge happened. That is observation, not validation, and it is what
  makes "we merged 40 PRs with no gates" visible after the fact.

## Consequences

- The core stays deterministic and small: no policy engine, no check taxonomy,
  no per-repo configuration to drift out of date. Gates appearing mid-run —
  node 1 creates the repo, node 5 turns on branch protection — need no special
  handling, because every settle asks the same question.
- A repo with no gates merges everything immediately. That is the operator's
  informed choice, stated plainly rather than papered over; autonomy scales
  with the rigour of the target repo, not with ticketflow's settings.
- ticketflow cannot catch gate weakening itself. Detection is deferred to the
  repo's own pipeline, and a team that skips the published guidance gets no
  compensating control from us.
- Diagnosing a bad merge means reading the host's records and our event log,
  not a ticketflow verdict — we can say what happened, never whether it was
  acceptable.
- The reference templates in `examples/` must be maintained as documentation
  without ever becoming a supported, imported surface.

## Review guidance

- Flag any code that parses PR diffs, inspects check findings, or classifies
  check names or CI jobs — ticketflow reads merge conclusions only.
- Flag any configuration schema, table or setting that describes the target
  repo's checks, or any preflight/conformance validation of repo settings
  before dispatch.
- Flag any write to the target repo outside branch and PR operations — workflow
  files, branch-protection settings, repo configuration (invariant 5).
- Require settle-loop changes to preserve the §9.1 ladder order (0–5), the
  batched settle window, and the auto-merge fallback before unconditional
  merge.
- Require the cycle cap to remain configurable with default 100 and to be
  counted separately from dispatch attempts.
- Require failed checks to be re-run exactly once before dispatching feedback,
  with per-check flake rates persisted to SQLite.
- Flag any dependency on gate-checking libraries (e.g. `diff-cover`,
  `semgrep`, `unidiff`) outside `examples/` — those templates run in the
  user's CI, not in ticketflow.
- Flag any logic that branches on PR author class (human vs agent) — gates and
  the merge ladder apply identically to both (spec §12.5).

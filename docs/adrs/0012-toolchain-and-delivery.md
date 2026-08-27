# ADR-0012: Toolchain and delivery: Python 3.14, uv, just, TDD, CI gates

- Status: Accepted
- Date: 2026-08-27
- Revision 2026-08-27: dev-group additions that only support the gates
  themselves — typing stubs (`types-*`) and similar zero-runtime tooling —
  are routine and need no ADR amendment; runtime dependencies always do.
- Revision 2026-08-27 (planner, ADR-0014): two runtime dependencies added —
  `pydantic-ai-slim[anthropic]` (MIT; planner synthesis, imported only in
  its adapter per ADR-0002) and `ruamel.yaml` (MIT; plan-file round-trip
  with comments). Both use compatible ranges: the exact-pin rule below
  covers only `githubkit` and the agent SDK/CLI packages, and `uv.lock`
  resolves the exact versions everywhere.

## Context

ticketflow's governing bet is that a repo safe to hand to an autonomous agent is
a repo that was already well run (spec §1.1): branch protection, required
checks, tests that fail for real reasons. ticketflow must be such a repo itself
— both because agents will work on it, and because a project that publishes
`REPO_REQUIREMENTS.md` (spec §8.5) while ignoring its own advice is not
credible. The orchestrator is deterministic Python by design (spec §3);
reproducibility is what makes crash-recovery, idempotency and unit tests mean
anything, and that determinism has to extend to the toolchain that builds and
verifies it.

The dependency stack is fixed by the spec (spec §15.1): permissive licences
throughout, with several dependencies pre-1.0 or fast-moving. The spec's pinning
guidance (spec §15.4) is explicit that `githubkit` and both agent SDKs must be
pinned exactly, and that adapters stay thin so an upgrade touches one file. This
ADR records the toolchain, recipe runner, quality gates and delivery pipeline
that enforce all of the above, complementing the review process defined in
ADR-0001.

## Decision

**Runtime and packaging.** The project targets Python 3.14 and is managed by
uv. `uv.lock` is committed and authoritative everywhere; CI installs with
`uv sync --locked` and fails on any drift between lock and manifest. The code
lives in a `src/` layout and builds with the hatchling backend.

**Recipes.** Developer and CI commands are `just` recipes in a `justfile` —
not Make. The recipe set is: `install`, `fmt`, `lint`, `typecheck`, `test`,
`cov`, `check`. CI entrypoints call the same recipes contributors run locally;
no CI job encodes a command a contributor cannot reproduce with `just`.

**Quality gates.** ruff provides both formatting and linting. mypy runs in
strict mode. pytest runs with coverage, and the coverage `fail_under` threshold
lives in `pyproject.toml`. TDD is the working method: a behaviour change lands
in the same PR as the test that demanded it, and a bug fix lands with a
regression test that fails without the fix.

**CI.** GitHub Actions carries the pipeline:

- `ci.yml` — the quality and test jobs, calling the `just` recipes.
- `adr-review.yml` — the ADR conformance review required by ADR-0001.
- `security.yml` — dependency audit via `pip-audit`.
- Dependabot covers the `uv` and `github-actions` ecosystems, with auto-merge
  enabled for patch and minor updates once the required checks pass.

**Branch protection.** `main` requires the `quality`, `test` and `adr-review`
status checks, requires conversation resolution, accepts changes only via pull
request, and is enforced for admins. Merges are squash merges.

Dev-group additions that only support the gates themselves — typing stubs
(`types-*`) and similar zero-runtime tooling — are routine and need no ADR
amendment (revision above); runtime dependencies always do (ADR-0001).

**Pinning.** Per spec §15.4: `githubkit` and the agent SDK/CLI packages
(`claude-agent-sdk`, `github-copilot-sdk`) are pinned exactly — they move fast
and track vendor schemas. Other direct dependencies use compatible ranges, with
`uv.lock` resolving the exact versions everywhere. GitHub Actions are pinned to
major tags. Runtime dependencies carry permissive licences only
(MIT/Apache-2.0/BSD), per spec §15. Git worktree operations therefore use the
`git` CLI via `subprocess` — the spec-sanctioned alternative to `pygit2`
(GPL-2 with linking exception), which is excluded to keep the licence policy
uniform.

## Consequences

- The repo satisfies its own gate-integrity contract (spec §8.1): required
  checks, PR-only changes and admin enforcement mean agents dispatched against
  ticketflow itself — including via ticketflow — are judged by real gates.
- One committed lockfile plus `uv sync --locked` makes every environment —
  contributor, CI, agent worktree — byte-identical, so "works on my machine"
  failures are excluded by construction.
- `just` as the single command surface means CI and local runs cannot diverge,
  but adds one bootstrap tool contributors must install before anything works.
- Exact pins on `githubkit` and the agent SDKs mean vendor changes never arrive
  silently; the cost is a steady stream of Dependabot PRs, mitigated by
  auto-merge for patch/minor once checks pass. Major upgrades still need a
  human, and the thin-adapter rule (spec §15.4) keeps that work to one file.
- mypy strict and the coverage threshold make small refactors slower to land;
  in exchange, the deterministic core stays provably typed and tested, which is
  what its correctness claims rest on (spec §3).
- Squash merges keep `main` linear and revertable per PR, at the cost of
  losing intra-branch history.
- Enforcement of the TDD rule beyond mechanical signals (coverage, test-diff
  presence) is deferred to human and ADR review; no tool can verify that the
  test came first.

## Review guidance

- Flag any PR that changes `pyproject.toml` dependencies without a
  corresponding `uv.lock` change, or that edits `uv.lock` by hand with no
  manifest change.
- Flag any pin of `githubkit`, `claude-agent-sdk` or `github-copilot-sdk` that
  is not exact (`==`), and any new runtime dependency whose licence is not
  MIT, Apache-2.0 or BSD (this excludes `pygit2`; git operations go through
  the `git` CLI).
- Flag CI workflow steps that invoke tools directly (`ruff`, `mypy`, `pytest`,
  `uv run ...`) instead of the corresponding `just` recipe, and any Makefile
  added to the repo.
- Flag workflow changes that install dependencies with anything other than
  `uv sync --locked`, or that reference a Python version other than 3.14.
- Flag GitHub Actions `uses:` references not pinned to a major tag.
- Require every PR that changes behaviour under `src/` to include test changes
  in the same diff; require bug-fix PRs to include a regression test.
- Flag any lowering of the coverage `fail_under` threshold, relaxation of mypy
  strictness, or newly added lint/type suppressions (`# noqa`,
  `# type: ignore`) without stated justification.
- Flag removal or renaming of the `quality`, `test` or `adr-review` jobs, since
  branch protection requires them by name.

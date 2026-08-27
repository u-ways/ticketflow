# ticketflow

[![CI](https://github.com/u-ways/ticketflow/actions/workflows/ci.yml/badge.svg)](https://github.com/u-ways/ticketflow/actions/workflows/ci.yml)

A local orchestrator that reads a dependency graph out of an issue tracker, dispatches
coding agents against the ready nodes, and lets the target repo's own CI and review
agents decide whether the work is acceptable.

**ticketflow adds no safety of its own. It inherits yours.** A repo that is safe to
hand to an autonomous agent is a repo that was already well run — branch protection,
required checks, tests that fail for real reasons. ticketflow asks the host one
question — *can this pull request be merged?* — and acts on the answer. See
[REPO_REQUIREMENTS.md](REPO_REQUIREMENTS.md) for what a well-configured target repo
provides.

## How it works

- **Trackers are the input.** Jira and GitHub Issues are both first-class. Dependencies
  are declared in the issue body with a `depends-on:` line; the tracker's native links
  are only a human-readable mirror.
- **The orchestrator is not an agent.** Scheduling is deterministic Python: a
  topological ready-set ([`graphlib`](https://docs.python.org/3/library/graphlib.html)),
  a lease table, and a reconcile tick. No model runs in the scheduling loop.
- **SQLite is truth.** Boards, traces and status views are projections and may lag.
- **Agents run as detached processes** in their own git worktrees, surviving
  orchestrator restarts. Startup adopts in-flight work; it does not clean it up.
- **The repo's gates are the quality signal.** ticketflow never parses a diff, never
  judges code, and cannot bypass branch protection under any flag.

The full design lives in [docs/architecture.md](docs/architecture.md), and every
binding decision is recorded as an ADR in [docs/adrs/](docs/adrs/).

## Demo

A reproducible end-to-end demo — seed a sandbox tracker with a four-ticket
dependency graph, run the orchestrator, watch agents take it to merged, then
reset. See [demo/README.md](demo/README.md).

```sh
just demo seed-github --repo owner/sandbox
uv run ticketflow run --config ticketflow.toml --yolo
just demo reset-github --repo owner/sandbox --state-dir .ticketflow
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just).

```sh
just install    # create the venv and install all dependencies
just check      # lint + typecheck + test (what CI runs)
just test       # pytest only
just fmt        # auto-format and auto-fix lint findings
```

Run `just` with no arguments to list every recipe.

### Working method

- **TDD.** A behaviour change lands with the test that demanded it; bug fixes land
  with a regression test.
- **ADR-driven.** Architecturally significant changes land in the same PR as the ADR
  that permits them. Every PR is reviewed by an automated ADR reviewer
  ([`adr-review.yml`](.github/workflows/adr-review.yml)) whose comments block merge
  until resolved. See [ADR-0001](docs/adrs/0001-record-architecture-decisions.md).

## Licence

[MIT](LICENSE)

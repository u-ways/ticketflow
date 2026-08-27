# ticketflow — agent guide

A local orchestrator that schedules coding agents from an issue tracker's
dependency graph. Deterministic Python core; agents run inside nodes only.

## The rules that bind every change

- **ADRs are the law.** `docs/adrs/` is the normative rulebook; every PR is
  reviewed against them by an automated ADR reviewer whose comments block
  merge. Architecturally significant changes land in the same PR as the ADR
  that permits them (ADR-0001). `docs/architecture.md` is background only.
- **TDD.** A behaviour change lands with the test that demanded it; bug
  fixes land with a regression test that fails without the fix.
- **The five invariants** (spec §2): SQLite is truth; human signals enter
  only through the intents table; adapters translate, never decide; every
  dispatch is idempotent and leased; the target repo is read-only except
  branches and PRs.
- No model or LLM call anywhere in `orchestrator`, `graph`, or `store`.
- No vendor SDK import outside `src/ticketflow/adapters/`.

## Commands

```sh
just install     # uv sync --locked --all-groups
just check       # lint + typecheck + tests with coverage (what CI runs)
just test -k x   # targeted pytest
just fmt         # ruff format + autofix
```

Run `just` for the full recipe list. CI calls these same recipes; never add
workflow steps that bypass them (ADR-0012).

## Layout

- `src/ticketflow/domain/` — canonical types, transition table, body parser
- `src/ticketflow/store/` — SQLite store (WAL, sequential migrations)
- `src/ticketflow/graph/` — ready-set/cycle/stagger pure functions
- `src/ticketflow/orchestrator/` — the reconcile tick and prompts
- `src/ticketflow/ports/` — the three port protocols (exactly three)
- `src/ticketflow/adapters/` — vendor adapters (the only SDK imports)
- `src/ticketflow/supervision/` — detached processes, run dirs, worktrees
- `tests/fakes.py` — port fakes; core tests never mock vendor SDKs

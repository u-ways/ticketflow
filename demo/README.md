# The ticketflow demo

A reproducible end-to-end demo: seed a tracker with a four-ticket dependency
graph (the "qacalc" epic — a diamond: one root, two parallel arms, one tip),
point ticketflow at it, and watch agents take every ticket from Backlog to
Merged. The same epic ran the project's first live QA and completed 4/4 with
zero escalations.

```
scaffold ──┬── operations ──┐
           └── cli ─────────┴── readme
```

## Prerequisites

- A **sandbox repository** you are happy to let agents push branches and merge
  PRs into (e.g. `u-ways/ticketflow-qa-sandbox`). Treat everything in it as
  disposable.
- `gh` logged in (`gh auth status`) with `repo` scope — plus `project` scope if
  you use a Projects v2 board, and `workflow` scope so agents can push CI
  workflow files (see "Findings" in the project README).
- The `claude` CLI installed and authenticated (the demo runner).
- For the Jira flavour: `ATLASSIAN_EMAIL` and `ATLASSIAN_API_TOKEN` exported,
  and a project whose issue type `Task` exists.

## 1. Seed the tracker

GitHub Issues (optionally mirrored onto a Projects v2 board):

```sh
just demo seed-github --repo u-ways/ticketflow-qa-sandbox \
    --project-owner u-ways --project-number 5
```

Jira:

```sh
just demo seed-jira --base-url https://u-ways.atlassian.net --project KAN
```

Issues are created in dependency order so each `depends-on:` line references
the real numbers/keys the tracker just assigned, and every issue carries the
`tf-demo` label so reset can find them later.

## 2. Configure and run

Write a config next to wherever you want the run state to live:

```toml
# ticketflow.toml
state_dir = ".ticketflow"

[tracker]
provider = "github"                     # or "jira"
repo = "u-ways/ticketflow-qa-sandbox"   # github provider
# base_url = "https://u-ways.atlassian.net"   # jira provider
# project_key = "KAN"                         # jira provider
project_owner = "u-ways"                # optional: Projects v2 board
project_number = 5

[codehost]
repo = "u-ways/ticketflow-qa-sandbox"

[runner]
name = "claude"
model = "sonnet"

[limits]
max_parallel = 2
attempt_timeout_seconds = 1500
```

Then, with `GITHUB_TOKEN` set (`export GITHUB_TOKEN=$(gh auth token)`):

```sh
uv run ticketflow run --config ticketflow.toml --yolo --interval 20
```

`--yolo` skips tool-permission prompts (ADR-0013) — appropriate for a
disposable sandbox; the repo's own gates still decide every merge. Watch
progress from another shell:

```sh
uv run ticketflow status --config ticketflow.toml       # node states
uv run ticketflow events --config ticketflow.toml       # the event log
uv run ticketflow escalations --config ticketflow.toml  # what needs a human
```

Or just watch the board: issues move Backlog → In progress → In review → Done,
and PRs appear, get judged by whatever checks the repo has, and merge.

To see the gates story properly, add branch protection to the sandbox after
the scaffold ticket merges its CI workflow — make the `test` check required
(and enforce it for admins, or use a non-admin token: an admin identity
bypasses required checks, which is exactly what REPO_REQUIREMENTS R6 warns
about). Later merges will then genuinely wait for green.

## 3. Reset

Close the demo issues, delete leftover `tf/*` branches, and drop the local
run state:

```sh
just demo reset-github --repo u-ways/ticketflow-qa-sandbox --state-dir .ticketflow
```

Jira (moves demo issues to Done; `--delete` removes them outright):

```sh
just demo reset-jira --base-url https://u-ways.atlassian.net --project KAN
```

Two things reset deliberately leaves alone:

- **The sandbox's default branch** keeps the merged demo work. To restore a
  pristine sandbox, temporarily lift branch protection and force-push the
  baseline commit:

  ```sh
  git clone https://github.com/u-ways/ticketflow-qa-sandbox && cd ticketflow-qa-sandbox
  git reset --hard <baseline-sha>   # e.g. the initial LICENSE-only commit
  git push --force origin main
  ```

- **Projects v2 board items** for closed issues stay on the board; archive
  them from the board UI (or leave them — the next seed adds fresh items).

Re-seeding after a reset gives you a clean, repeatable demo loop.

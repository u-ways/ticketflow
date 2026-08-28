# The ticketflow demo

Two reproducible demos against a sandbox repo, one epic either way:

- **The planner demo** (the showcase, ADR-0014): seed ONE underspecified
  epic, let the planner ground it, propose a decomposition with per-edge
  confidence and evidence, review and approve it, emit real child tickets —
  then watch the orchestrator execute the graph the planner wrote.
- **The pre-wired demo** (the quick path): seed the four-ticket qacalc
  diamond directly and go straight to execution. This is the epic that ran
  the project's first live QA, 4/4 merged with zero escalations.

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

## 1a. The planner demo (showcase)

Seed one deliberately underspecified epic:

```sh
just demo seed-epic-github --repo u-ways/ticketflow-qa-sandbox \
    --project-owner u-ways --project-number 5
# or: just demo seed-epic-jira --base-url https://u-ways.atlassian.net --project KAN
```

Add a `[planner]` section to the config below, then decompose it. With a
Claude subscription and no API key, use the CLI synthesis backend:

```toml
[planner]
synthesis_backend = "claude-cli"   # pydantic-ai + synthesis_model needs ANTHROPIC_API_KEY
grounding_model = "sonnet"
synthesis_model = "sonnet"
```

```sh
uv run ticketflow plan new "#N"              # ground the epic, propose a plan
uv run ticketflow plan show "#N"             # items; edges ascending by confidence
uv run ticketflow plan revise "#N" --feedback "Split the CLI into its own item."
uv run ticketflow plan edit "#N"             # or hand-edit plans/<epic>.yaml in $EDITOR
uv run ticketflow plan approve "#N"
uv run ticketflow plan emit "#N"             # child tickets appear, wired with depends-on
```

(`plan new "#N" --yolo` collapses all of that into one command — grounding,
synthesis, auto-approval, emission.) Review is mostly pruning: edges print
least-evidenced first, and unevidenced proposals are listed apart. The
emitted children carry `tf-plan-<plan-id>` labels, real `depends-on:` lines,
and sub-issue/link mirrors; from here the run step below executes them like
any human-authored graph.

## 1a′. The board-first variant (GitHub Projects)

The same planner arc, but the demo creates its OWN Project board and the
board is the audience surface: the epic starts there alone, and every
planner-emitted child is auto-added as its state first projects — you watch
the board populate and move Backlog → In progress → Done live.

```sh
just demo seed-project-github --repo u-ways/ticketflow-qa-sandbox --owner u-ways
```

It prints the `[tracker]` config for the fresh board (a default Status field
is all it needs). Plan, approve, emit and run exactly as above; native
blocked-by relationships and the sub-issue hierarchy under the epic land as
mirrors. Reset can take the board with it:

```sh
just demo reset-github --repo u-ways/ticketflow-qa-sandbox \
    --state-dir .ticketflow --delete-project <N> --project-owner u-ways
```

## 1b. The pre-wired demo (quick path)

Seed the four-ticket diamond directly — created in dependency order so each
`depends-on:` line references the real numbers/keys the tracker assigned:

```sh
just demo seed-github --repo u-ways/ticketflow-qa-sandbox \
    --project-owner u-ways --project-number 5
# or: just demo seed-jira --base-url https://u-ways.atlassian.net --project KAN
```

Everything seeded either way carries the `tf-demo` label (children of a plan
carry its `tf-plan-*` label) so reset can find it all later.

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

Reset also closes planner-emitted children (`tf-plan-*` labels on GitHub, the
body marker on Jira). The `plans/` working copies are the reviewed artifacts
and are left in place; a new plan for the same epic key overwrites its file.

Re-seeding after a reset gives you a clean, repeatable demo loop.

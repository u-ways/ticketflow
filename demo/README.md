# The ticketflow demos

One epic, four ways to run it. Everything targets a **disposable sandbox
repo**; all demo issues carry `tf-demo` (children of a plan: `tf-plan-*`).

**Prerequisites (once):**

- `gh auth status` logged in, scopes `repo`, `project`, `workflow`.
- `claude` CLI authenticated (runner + planner synthesis, no API key needed).
- `export GITHUB_TOKEN=$(gh auth token)` in the shell that runs ticketflow.
- A `ticketflow.toml`:

```toml
state_dir = ".ticketflow"
plans_dir = "plans"

[tracker]
provider = "github"
repo = "OWNER/sandbox"
project_owner = "OWNER"      # optional: Projects v2 board
project_number = 5

[codehost]
repo = "OWNER/sandbox"

[runner]
model = "sonnet"

[planner]
synthesis_backend = "claude-cli"
grounding_model = "sonnet"
synthesis_model = "sonnet"
```

## 1 · Planner demo (showcase)

One vague epic; the planner decomposes it, you review, agents execute.

```sh
just demo seed-epic-github --repo OWNER/sandbox --project-owner OWNER --project-number 5
uv run ticketflow plan new '#N'                       # ground + propose (minutes)
uv run ticketflow plan show '#N'                      # edges ranked by confidence
uv run ticketflow plan revise '#N' --feedback '...'   # optional; or: plan edit '#N'
uv run ticketflow plan approve '#N'
uv run ticketflow plan emit '#N'                      # wired child tickets appear
uv run ticketflow run --yolo                          # agents execute the graph
```

`plan new '#N' --yolo` collapses plan/approve/emit into one command.

## 2 · Planner demo, board-first (fresh GitHub Project)

Same arc, but the demo creates its own Project board; the epic starts there
and children land on it live as their states project.

```sh
just demo seed-project-github --repo OWNER/sandbox --owner OWNER
# paste the printed [tracker] block into ticketflow.toml, then:
uv run ticketflow plan new '#N' --yolo
uv run ticketflow run --yolo
```

## 3 · Pre-wired graph (quick path, no planner)

The four-ticket diamond, `depends-on:` already wired.

```sh
just demo seed-github --repo OWNER/sandbox --project-owner OWNER --project-number 5
uv run ticketflow run --yolo
```

## 4 · Jira

Either flavour against a Jira project (needs `ATLASSIAN_EMAIL` +
`ATLASSIAN_API_TOKEN`; config: `provider = "jira"`, `base_url`,
`project_key`).

```sh
just demo seed-epic-jira --base-url https://SITE.atlassian.net --project KEY   # planner
just demo seed-jira      --base-url https://SITE.atlassian.net --project KEY   # pre-wired
```

## Watching

```sh
uv run ticketflow status          # node states
uv run ticketflow escalations     # what needs a human (resolve: retry/cancel)
uv run ticketflow events          # the append-only log
```

## Reset

```sh
just demo reset-github --repo OWNER/sandbox --state-dir .ticketflow \
    [--delete-project N --project-owner OWNER]        # board-first demo cleanup
just demo reset-jira --base-url https://SITE.atlassian.net --project KEY [--delete]
```

Closes demo issues (planner children included), deletes `tf/*` branches,
removes local run state. The sandbox's default branch keeps merged work — to
restore it: force-push the baseline commit (lift branch protection first).
`plans/` files are the reviewed artifacts and stay.

## Notes

- `--yolo` skips tool-permission prompts only; the repo's own gates still
  decide every merge. Fine for a disposable sandbox.
- Planner turns are resumable: if `plan new` is interrupted, re-running
  continues from where it stopped. Synthesis can take several minutes.
- To see real gates: after the CI child merges, make its check required on
  the sandbox (and enforce for admins, or use a non-admin token — R6).

# ADR-0010: Detached-process supervision with adoption on restart

- Status: Accepted
- Date: 2026-08-27

## Context

ticketflow must survive orchestrator restarts without losing in-flight agent
work (spec §1). Agent attempts run for minutes to hours; if the orchestrator's
death took its children with it, every restart would forfeit work already paid
for. The runner SDKs will happily spawn and manage the agent CLI themselves,
but an SDK-owned child dies with its parent (spec §7.2), so ownership of the
process must stay with us. The baseline must also work anywhere — no daemon,
no service manager as a hard dependency (spec §10, §15.2).

Agent stdout/stderr is almost all the bytes the system produces, while the
metadata around it is kilobytes (spec §12.7). Retention therefore has to be
split by class, and it has to respect liveness: epics run longer than any
age-based sweep, and downstream nodes read upstream handoffs (spec §12.2).

## Decision

Run every agent attempt as a plain detached process. Startup adopts existing
work; it never cleans up.

- Detach the child with `setsid` (or double-fork) so it leaves the
  orchestrator's process group. Orchestrator death — including SIGHUP on exit —
  never kills a running agent. Nothing else kills a running agent either,
  except the per-attempt runaway guard below or a human cancel intent routed
  through the intents table (ADR-0004) into `RunnerPort.cancel` (ADR-0011).
- Give each attempt a run directory `runs/<node_id>/<attempt>/` containing
  `meta.json` (pid, start_time, runner, model, session_id, trace_id),
  `stdout.log`, `stderr.log`, `exit_code`, and `heartbeat`.
- Capture the exit code of a process we are not waiting on via
  `sh -c 'agent ...; echo $? > exit_code'`. The existence of the `exit_code`
  file is the completion signal; its contents are the result.
- Store the pid **and** the process create_time (via `psutil`), and validate
  both before trusting a pid. PIDs get reused; a bare pid is not an identity.
- On startup, scan the run directories for every attempt the database
  (ADR-0003) calls in-flight: re-attach the ones whose `(pid, create_time)`
  pair is live, harvest the ones whose `exit_code` file exists, and expire the
  rest so their leases roll back per ADR-0008.
- Detect the workspace per attempt; never configure it. If the target repo
  exists, create a git worktree branched from the default branch. If it does
  not, hand the agent an empty directory — it scaffolds, initialises and
  pushes (the bootstrap case) — and later attempts take the worktree path
  automatically because the repo now exists.
- Never read success from stdout. Judge an attempt by, in order: the exit
  code, then the repo's checks (ADR-0009), then a non-empty
  `git diff --stat`. A clean exit with an empty diff escalates (ADR-0006).
- Enforce a per-attempt runaway guard: a hard wall-clock ceiling and a token
  ceiling. These terminate a stuck loop; they do not manage spend
  (ADR-0013). Beyond the guard and a human cancel intent (ADR-0004), no
  other mechanism kills a running agent.
- Apply retention by class, per the spec's table (§12.7): agent stdout/stderr
  for 14 days or a 10 GB total cap, whichever fires first; `meta.json`, exit
  codes, research briefs, plan revisions and handoffs for 12 months; the
  event log (ADR-0005) is never deleted.
- Never delete anything belonging to a non-terminal run, at any age. Liveness
  beats age.
- Cap each attempt's raw log at 500 MB, truncating the middle rather than the
  tail — the end is where the failure is. Gzip logs on attempt completion.
  Log every truncation and eviction to the event log; history must never
  vanish silently.
- Offer `systemd-run --user` (journald logs, `MemoryMax`/`CPUQuota`) as an
  optional upgrade behind the same runner port (ADR-0011), selected by
  config. It is never a dependency.

## Consequences

Easier: orchestrator restarts and upgrades become routine — adoption
reconstructs supervision state from run directories plus the database, so no
in-flight work is lost. Crash recovery needs no workflow engine (the spec
rejects DBOS, Temporal and friends for exactly this reason, §15.2). The
bootstrap case needs no special mode: workspace detection makes "the repo does
not exist yet" an ordinary attempt.

Harder: we own supervision ourselves — pid-reuse validation, heartbeat
handling and the adoption scan are our code and our bugs, where a daemon
would have provided them. Detached processes cannot be waited on, so
completion detection rests entirely on the `exit_code` file convention, and
a runner that bypasses the `sh -c` wrapper breaks it. Resource caps
(`MemoryMax`, `CPUQuota`) are only available on the optional systemd path,
so the portable baseline has no memory or CPU ceiling — only wall-clock and
tokens.

Deferred: tuning the retention caps and ceilings from real runs (spec §16),
and any richer supervision (cgroups everywhere, journald by default) beyond
the optional systemd upgrade.

## Review guidance

- Flag any spawn of an agent process that does not detach via `setsid` or
  double-fork, or that lets a runner SDK own the child process.
- Flag any code that stores or compares a bare pid without its
  `create_time`; require `(pid, create_time)` validation before any signal,
  poll or kill.
- Flag startup or reconcile code that deletes, kills or resets run
  directories or processes instead of adopting them (re-attach, harvest, or
  expire only).
- Flag any success determination that parses agent stdout; require the
  exit-code → checks → `git diff --stat` order, with escalation on a clean
  exit with an empty diff.
- Flag any kill path for a running agent other than the wall-clock or token
  runaway guard, or a cancel intent routed through the intents table
  (ADR-0004) into `RunnerPort.cancel` (ADR-0011).
- Flag retention changes that delete artifacts of a non-terminal run, drop
  events from the event log, or alter the 14-day/10 GB, 12-month or 500 MB
  values without updating this ADR.
- Require log truncation to remove the middle of the file, and require every
  truncation or eviction to emit an event.
- Flag `systemd-run` usage that is not behind the runner port and a config
  switch, or that any default code path depends on.

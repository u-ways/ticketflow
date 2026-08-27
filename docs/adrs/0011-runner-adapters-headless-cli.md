# ADR-0011: Runner adapters: headless CLI first, SDK client attach later

- Status: Accepted
- Date: 2026-08-27

## Context

The core dispatches coding agents through the RunnerPort, one of the three
vendor ports of ADR-0002, and both first-class runners — Claude Code and GitHub
Copilot CLI — ship official Python SDKs that bundle their CLIs (spec §15.1). The
spec's preferred integration is to spawn the agent CLI in server mode as our own
detached process and attach the SDK to it as a client, so that supervision stays
with the orchestrator while protocol handling stays with the vendor (spec §7.2).
Letting the SDK own the child process is ruled out either way: orchestrator
death would kill in-flight agents, defeating the adoption model of ADR-0010.

Runners also differ in ways the core must not learn. Cost units differ per
runner — Claude bills tokens, Copilot counts prompts against an allowance (spec
§12.3) — and provider quota exhaustion is a hard wall that fails every in-flight
node at once, which treated as ordinary failure burns every node's retry
allowance in seconds (spec §12.3). Both SDKs move weekly and must be pinned
exactly, with adapters kept thin so an upgrade touches one file (spec §15.4).

This ADR fixes the port shape, the first (M1) adapter implementation, and the
obligations any later adapter must meet.

## Decision

Implement runner adapters against the async-shaped RunnerPort. The port is:

- `start(node, workspace, policy) -> handle`
- `poll(handle) -> running | exited(code) | timed_out`
- `resume(handle, feedback) -> handle`
- `cancel(handle)`
- `capabilities()`

Keep the port async-shaped deliberately, so a remote assign-issue-and-wait
runner fits later without reshaping the core.

**M1 deviates from the spec, and records the deviation here.** The spec prefers
spawning the CLI in server mode and attaching the official SDK as a client. M1
instead spawns the `claude` CLI directly in headless print mode (`claude -p`,
`--output-format stream-json`) as the detached `setsid` process of ADR-0010,
capturing the session id from the stream so the attempt can be resumed.
Rationale: supervision stays ours either way; the SDK client attach adds a
resident protocol bridge with no M1 payoff; and resume-with-feedback works
headless via `--resume <session_id>`. Revisit this deviation when the Copilot
adapter lands, or when per-tool-call permission callbacks are needed.

The following rules bind every adapter:

- **ToolPolicy is core-owned.** M1 compiles it to `--allowedTools` /
  `--disallowedTools` CLI flags; the upgrade path is SDK permission callbacks,
  which see the actual tool arguments rather than a static allowlist. The
  Copilot SDK defaults to permissive, so its adapter must always install a
  policy — recorded now so the future adapter cannot skip it.
- **Model is pinned per node class**, read from config. An adapter never
  hardcodes a model.
- **Quota and rate-limit errors are a distinct error class.** On detection,
  pause dispatch and surface the reason instead of consuming retry attempts.
  Resume is an intent, entering through the intents table per ADR-0004.
- **The adapter normalizes cost at its boundary.** Cost units differ per runner
  (tokens versus prompt allowance); the core never assumes tokens.
- **The Copilot CLI adapter is deferred, but the port must keep it honest.** A
  second fake runner exercises every port method in tests, so the port cannot
  drift into Claude-shaped assumptions.
- **BYOK caution, carried from the spec:** never point the worker and a
  repo-side policy reviewer at the same underlying model — that reintroduces
  the correlated blindness the CI-side gate exists to avoid (ADR-0009).

## Consequences

- M1 ships with one dependency fewer and no resident bridge process: the
  adapter is a thin command-line compiler plus a stream parser, which is easy
  to test and easy to replace.
- Resume-with-feedback works from day one via `--resume <session_id>`, so the
  review loop does not wait for the SDK integration.
- Tool policy enforcement is coarser in M1: CLI flags cannot inspect actual
  tool arguments, so per-call judgement is deferred until the SDK client
  attach lands. This is an accepted, temporary weakening.
- The stream-json output format and CLI flags are a less stable interface than
  the SDK; exact pinning (spec §15.4) and a thin adapter contain the blast
  radius of a CLI change.
- The Copilot adapter is deferred, so runner-agnosticism is only proven by the
  second fake runner until it lands. The permissive-by-default Copilot SDK is
  a recorded trap for that future work, not a solved problem.
- Quota pauses stop the scheduler burning retries during an outage, at the
  cost of a dispatch stall that requires a human intent to clear.

## Review guidance

- Flag any change to the RunnerPort signature that removes or reshapes
  `start`, `poll`, `resume`, `cancel` or `capabilities`, or makes the port
  synchronous/blocking.
- Flag any adapter code that imports `claude_agent_sdk` or spawns the CLI in
  server mode without a corresponding update to this ADR's status.
- Flag a hardcoded model identifier (e.g. strings starting `claude-` or
  `gpt-`) inside a runner adapter; require the model to come from config keyed
  by node class.
- Require the Claude adapter to pass `--allowedTools`/`--disallowedTools`
  derived from the core ToolPolicy on every `start`, except when the run was
  started with `--yolo` (ADR-0013); flag any other spawn path that omits them.
- Flag scheduling, retry or state-transition logic inside a runner adapter;
  adapters translate, they never decide (ADR-0002).
- Require quota and rate-limit errors to map to the distinct quota error
  class; flag handling that routes them into the generic retry path.
- Require any change to the RunnerPort to be exercised by both fake runners in
  the test suite; flag a port change with only one fake updated.
- Flag cost figures crossing the port in runner-native units; require
  normalization inside the adapter.

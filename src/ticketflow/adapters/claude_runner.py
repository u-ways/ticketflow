"""Claude Code CLI runner adapter (ADR-0011).

Drives the ``claude`` CLI headless (``claude -p --output-format stream-json``)
as the detached ``setsid`` process of ADR-0010, through the supervision layer.
The adapter is a thin command-line compiler plus a stream parser: it translates
between the CLI and the RunnerPort DTOs and never decides scheduling, retries,
or state transitions (ADR-0002).

Cost normalization (ADR-0011): the CLI's ``result`` event reports
``total_cost_usd`` — already US dollars, which IS the normalized unit this
adapter emits as ``PollResult.cost``.

The model is never hardcoded here: ``start`` uses the dispatch's pinned model
and ``resume`` uses the configured one (ADR-0011).
"""

import gzip
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ticketflow.config import Limits, RunnerConfig
from ticketflow.ports.runner import (
    AttemptStatus,
    FailureClass,
    NodeDispatch,
    PollResult,
    RunnerCapabilities,
    RunnerHandle,
    ToolPolicy,
)
from ticketflow.supervision.process import is_alive, kill, spawn_detached
from ticketflow.supervision.run_dirs import RunDir

_TAIL_CHARS = 4000
"""How much of the combined stdout+stderr tail is scanned for quota markers."""

_QUOTA_MARKERS = ("rate limit", "usage limit", "quota", "credit balance", "overloaded", "429")
"""Case-insensitive markers mapping a failure to the distinct quota class (ADR-0011)."""


def build_command(
    prompt: str,
    *,
    model: str | None,
    policy: ToolPolicy,
    resume_session: str | None,
    binary: str,
) -> list[str]:
    """Compile one attempt's CLI invocation (ADR-0011). Pure; no I/O.

    The core-owned ToolPolicy compiles to ``--allowedTools``/``--disallowedTools``
    flags, except under yolo (ADR-0013) which skips permissions entirely. The
    model always arrives from the dispatch or config — never hardcoded.
    """
    command = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if model is not None:
        command += ["--model", model]
    if resume_session is not None:
        command += ["--resume", resume_session]
    if policy.yolo:
        command.append("--dangerously-skip-permissions")
    else:
        if policy.allowed_tools:
            command += ["--allowedTools", *policy.allowed_tools]
        if policy.disallowed_tools:
            command += ["--disallowedTools", *policy.disallowed_tools]
    return command


def _parse_stream(stdout: str) -> tuple[str | None, float | None]:
    """Extract ``(session_id, cost_usd)`` from the stream-json stdout.

    The session id comes from the first ``system``/``init`` event; the cost
    from the ``result`` event's ``total_cost_usd`` (already USD, ADR-0011).
    Non-JSON lines are skipped: the stream is a less stable interface than an
    SDK and partial logs (a killed attempt) must still parse.
    """
    session_id: str | None = None
    cost: float | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        if session_id is None and event.get("type") == "system" and event.get("subtype") == "init":
            raw_session = event.get("session_id")
            if isinstance(raw_session, str):
                session_id = raw_session
        elif event.get("type") == "result":
            raw_cost = event.get("total_cost_usd")
            if isinstance(raw_cost, int | float):
                cost = float(raw_cost)
    return session_id, cost


def _output_tokens_from_stream(stdout: str) -> int:
    """Sum output tokens reported by the stream's usage payloads (ADR-0010).

    The token half of the runaway guard: it terminates a stuck loop, it does
    not manage spend (ADR-0013).
    """
    total = 0
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, dict) else event.get("usage")
        if isinstance(usage, dict):
            tokens = usage.get("output_tokens")
            if isinstance(tokens, int):
                total += tokens
    return total


def _classify_failure(stdout: str, stderr: str) -> FailureClass:
    """Map a non-zero exit to QUOTA or ERROR (ADR-0011).

    Quota is a distinct class so the core pauses dispatch instead of burning
    retries; only the tail of the combined output is scanned.
    """
    tail = (stdout + stderr)[-_TAIL_CHARS:].lower()
    if any(marker in tail for marker in _QUOTA_MARKERS):
        return FailureClass.QUOTA
    return FailureClass.ERROR


def _read_log(path: Path) -> str:
    """Read a run-dir log, falling back to its gzipped form (ADR-0010)."""
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    compressed = path.with_suffix(path.suffix + ".gz")
    if compressed.is_file():
        with gzip.open(compressed, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return ""


class ClaudeRunner:
    """RunnerPort adapter spawning the Claude Code CLI headless (ADR-0011).

    Attempts are detached processes supervised through run dirs (ADR-0010);
    ``binary`` is the injectable seam so tests substitute a scripted
    executable and never hit the network.
    """

    def __init__(
        self,
        runner_config: RunnerConfig,
        limits: Limits,
        clock: Callable[[], datetime],
        binary: str = "claude",
        yolo: bool = False,
    ) -> None:
        self._config = runner_config
        self._limits = limits
        self._clock = clock
        self._binary = binary
        self._yolo = yolo
        """Per-run flag (ADR-0013): resume compiles the same policy start
        received, so a yolo run never regains permission prompts mid-loop."""

    def start(self, node: NodeDispatch, workspace: Path, policy: ToolPolicy) -> RunnerHandle:
        """Spawn one detached attempt; the prompt is recorded in the run dir."""
        run_dir = RunDir(node.run_dir).create()
        run_dir.prompt_path.write_text(node.prompt, encoding="utf-8")
        command = build_command(
            node.prompt, model=node.model, policy=policy, resume_session=None, binary=self._binary
        )
        pid, create_time = self._spawn(command, cwd=workspace, run_dir=run_dir)
        self._write_meta(
            run_dir,
            node_id=node.node_id,
            attempt=node.attempt,
            pid=pid,
            create_time=create_time,
            model=node.model,
            workspace=workspace,
        )
        return RunnerHandle(
            node_id=node.node_id,
            attempt=node.attempt,
            pid=pid,
            create_time=create_time,
            run_dir=node.run_dir,
            session_id=None,
            workspace=workspace,
        )

    def poll(self, handle: RunnerHandle) -> PollResult:
        """Report the attempt's status; translation only, no decisions.

        Harvested results are cached in ``meta.json`` so poll stays idempotent
        after the logs are compressed. On timeout the adapter only reports
        TIMED_OUT — killing is the orchestrator's call via ``cancel``.
        """
        run_dir = RunDir(handle.run_dir)
        meta = run_dir.read_meta()
        cached = meta.get("result")
        if cached is not None:
            return self._hydrate(cached)
        exit_code = run_dir.read_exit_code()
        if exit_code is not None:
            return self._finalize(run_dir, meta, exit_code, forced=None)
        if is_alive(handle.pid, handle.create_time):
            run_dir.heartbeat_path.touch()
            started_at = datetime.fromisoformat(str(meta["started_at"]))
            elapsed = (self._clock() - started_at).total_seconds()
            if elapsed > self._limits.attempt_timeout_seconds:
                return PollResult(status=AttemptStatus.TIMED_OUT, guard_reason="wall-clock timeout")
            tokens = _output_tokens_from_stream(_read_log(run_dir.stdout_path))
            if tokens > self._limits.attempt_token_ceiling:
                return PollResult(
                    status=AttemptStatus.TIMED_OUT,
                    guard_reason=f"token ceiling exceeded ({tokens} output tokens)",
                )
            return PollResult(status=AttemptStatus.RUNNING)
        # Dead with no exit_code file: the group was SIGKILLed before the
        # wrapper could record the code (ADR-0010) — synthesize a crash.
        return self._finalize(run_dir, meta, exit_code=1, forced=FailureClass.ERROR)

    def resume(self, handle: RunnerHandle, feedback: str) -> RunnerHandle:
        """Resume the original session with feedback (``--resume``, ADR-0011).

        ``handle`` is a template for the new attempt (pid 0; run_dir, session
        and workspace set by the orchestrator). Without a session id the
        adapter falls back to a cold start: the same command minus
        ``--resume``, losing conversational context but not the worktree.
        """
        if handle.workspace is None:
            raise ValueError("resume requires a workspace on the template handle")
        run_dir = RunDir(handle.run_dir).create()
        run_dir.prompt_path.write_text(feedback, encoding="utf-8")
        policy = ToolPolicy(
            allowed_tools=self._config.allowed_tools,
            disallowed_tools=self._config.disallowed_tools,
            yolo=self._yolo,
        )
        command = build_command(
            feedback,
            model=self._config.model,
            policy=policy,
            resume_session=handle.session_id,
            binary=self._binary,
        )
        pid, create_time = self._spawn(command, cwd=handle.workspace, run_dir=run_dir)
        self._write_meta(
            run_dir,
            node_id=handle.node_id,
            attempt=handle.attempt,
            pid=pid,
            create_time=create_time,
            model=self._config.model,
            workspace=handle.workspace,
        )
        return RunnerHandle(
            node_id=handle.node_id,
            attempt=handle.attempt,
            pid=pid,
            create_time=create_time,
            run_dir=handle.run_dir,
            session_id=handle.session_id,
            workspace=handle.workspace,
        )

    def cancel(self, handle: RunnerHandle) -> None:
        """Kill the attempt's process group after identity validation (ADR-0010)."""
        kill(handle.pid, handle.create_time)

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(supports_resume=True, reports_cost=True)

    def _spawn(self, command: Sequence[str], *, cwd: Path, run_dir: RunDir) -> tuple[int, float]:
        """The single seam to the vendor CLI; everything around it translates."""
        return spawn_detached(command, cwd=cwd, run_dir=run_dir)

    def _write_meta(
        self,
        run_dir: RunDir,
        *,
        node_id: str,
        attempt: int,
        pid: int,
        create_time: float,
        model: str | None,
        workspace: Path,
    ) -> None:
        run_dir.write_meta(
            {
                "node_id": node_id,
                "attempt": attempt,
                "pid": pid,
                "create_time": create_time,
                "runner": self._config.name,
                "model": model,
                "workspace": str(workspace),
                "started_at": self._clock().isoformat(),
            }
        )

    def _finalize(
        self, run_dir: RunDir, meta: dict[str, Any], exit_code: int, forced: FailureClass | None
    ) -> PollResult:
        """Parse the finished attempt, compress its logs, cache the result."""
        stdout = _read_log(run_dir.stdout_path)
        stderr = _read_log(run_dir.stderr_path)
        session_id, cost = _parse_stream(stdout)
        if forced is not None:
            failure = forced
        elif exit_code == 0:
            failure = FailureClass.NONE
        else:
            failure = _classify_failure(stdout, stderr)
        run_dir.compress_logs()
        result = {
            "exit_code": exit_code,
            "failure_class": failure.value,
            "session_id": session_id,
            "cost": cost,
        }
        meta["result"] = result
        run_dir.write_meta(meta)
        return self._hydrate(result)

    def _hydrate(self, result: dict[str, Any]) -> PollResult:
        """Rebuild a PollResult from the cached result dict in meta.json."""
        raw_exit = result.get("exit_code")
        raw_session = result.get("session_id")
        raw_cost = result.get("cost")
        return PollResult(
            status=AttemptStatus.EXITED,
            exit_code=int(raw_exit) if raw_exit is not None else None,
            failure_class=FailureClass(result["failure_class"]),
            session_id=raw_session if isinstance(raw_session, str) else None,
            cost=float(raw_cost) if isinstance(raw_cost, int | float) else None,
        )

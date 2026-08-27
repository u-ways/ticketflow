"""Claude runner adapter tests (ADR-0011): a scripted fake ``claude`` binary,
real detached processes, no network."""

import os
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ticketflow.adapters.claude_runner import ClaudeRunner, build_command
from ticketflow.config import Limits, RunnerConfig
from ticketflow.ports.runner import (
    AttemptStatus,
    FailureClass,
    NodeDispatch,
    RunnerHandle,
    ToolPolicy,
)
from ticketflow.supervision.process import is_alive
from ticketflow.supervision.run_dirs import RunDir

INIT_LINE = '{"type":"system","subtype":"init","session_id":"sess-abc"}'
RESULT_LINE = '{"type":"result","total_cost_usd":0.42}'


def wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def make_fake_claude(tmp_path: Path, body: str) -> str:
    """Write an executable fake ``claude`` and return its path (the seam)."""
    script = tmp_path / "fake-claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


def success_body(tmp_path: Path) -> str:
    argfile = tmp_path / "args.txt"
    lines = (INIT_LINE, "not json at all", "{malformed json", RESULT_LINE)
    echoes = "\n".join(f"echo '{line}'" for line in lines)
    return f"printf '%s\\n' \"$@\" > {argfile}\n{echoes}\nexit 0\n"


def sleeper_body() -> str:
    return f"echo '{INIT_LINE}'\nsleep 30\n"


def make_runner(binary: str, *, timeout_seconds: int = 3600) -> ClaudeRunner:
    return ClaudeRunner(
        RunnerConfig(model="cfg-model", allowed_tools=("Read",)),
        Limits(attempt_timeout_seconds=timeout_seconds),
        clock=lambda: datetime.now(UTC),
        binary=binary,
    )


def make_dispatch(tmp_path: Path, model: str | None = None) -> NodeDispatch:
    return NodeDispatch(
        node_id="n1", attempt=1, prompt="do the thing", run_dir=tmp_path / "runs/n1/1", model=model
    )


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


class TestBuildCommand:
    def test_base_shape(self) -> None:
        command = build_command(
            "p", model=None, policy=ToolPolicy(), resume_session=None, binary="claude"
        )
        assert command == ["claude", "-p", "p", "--output-format", "stream-json", "--verbose"]

    def test_model_from_dispatch_is_appended(self) -> None:
        command = build_command(
            "p", model="pinned", policy=ToolPolicy(), resume_session=None, binary="claude"
        )
        assert command[6:] == ["--model", "pinned"]

    def test_resume_session_is_appended(self) -> None:
        command = build_command(
            "p", model=None, policy=ToolPolicy(), resume_session="sess-abc", binary="claude"
        )
        assert command[6:] == ["--resume", "sess-abc"]

    def test_policy_compiles_to_allow_and_disallow_flags(self) -> None:
        policy = ToolPolicy(allowed_tools=("Read", "Edit"), disallowed_tools=("Bash",))
        command = build_command("p", model=None, policy=policy, resume_session=None, binary="c")
        assert command[6:] == ["--allowedTools", "Read", "Edit", "--disallowedTools", "Bash"]

    def test_yolo_skips_permissions_and_ignores_tool_lists(self) -> None:
        policy = ToolPolicy(allowed_tools=("Read",), disallowed_tools=("Bash",), yolo=True)
        command = build_command("p", model=None, policy=policy, resume_session=None, binary="c")
        assert command[6:] == ["--dangerously-skip-permissions"]
        assert "--allowedTools" not in command
        assert "--disallowedTools" not in command


class TestStart:
    def test_records_prompt_meta_and_spawns_detached(self, tmp_path: Path) -> None:
        binary = make_fake_claude(tmp_path, success_body(tmp_path))
        runner = make_runner(binary)
        workspace = make_workspace(tmp_path)
        node = make_dispatch(tmp_path, model="pinned")
        handle = runner.start(node, workspace, ToolPolicy(allowed_tools=("Read",)))

        run_dir = RunDir(node.run_dir)
        assert run_dir.prompt_path.read_text() == "do the thing"
        meta = run_dir.read_meta()
        assert meta["node_id"] == "n1"
        assert meta["attempt"] == 1
        assert meta["pid"] == handle.pid
        assert meta["create_time"] == handle.create_time
        assert meta["runner"] == "claude"
        assert meta["model"] == "pinned"
        assert meta["workspace"] == str(workspace)
        assert datetime.fromisoformat(meta["started_at"]).tzinfo is not None
        assert handle.session_id is None
        assert handle.workspace == workspace

        wait_for(lambda: run_dir.exit_code_path.is_file())
        args = (tmp_path / "args.txt").read_text().splitlines()
        assert args[:5] == ["-p", "do the thing", "--output-format", "stream-json", "--verbose"]
        assert args[5:] == ["--model", "pinned", "--allowedTools", "Read"]


class TestPoll:
    def _finished_handle(self, tmp_path: Path, body: str) -> tuple[ClaudeRunner, RunnerHandle]:
        runner = make_runner(make_fake_claude(tmp_path, body))
        node = make_dispatch(tmp_path)
        handle = runner.start(node, make_workspace(tmp_path), ToolPolicy())
        wait_for(lambda: RunDir(node.run_dir).exit_code_path.is_file())
        return runner, handle

    def test_parses_session_cost_and_exit_then_compresses(self, tmp_path: Path) -> None:
        runner, handle = self._finished_handle(tmp_path, success_body(tmp_path))
        result = runner.poll(handle)
        assert result.status is AttemptStatus.EXITED
        assert result.exit_code == 0
        assert result.failure_class is FailureClass.NONE
        assert result.session_id == "sess-abc"
        assert result.cost == pytest.approx(0.42)
        run_dir = RunDir(handle.run_dir)
        assert (run_dir.path / "stdout.log.gz").is_file()
        assert not run_dir.stdout_path.exists()

    def test_poll_is_idempotent_after_harvest(self, tmp_path: Path) -> None:
        runner, handle = self._finished_handle(tmp_path, success_body(tmp_path))
        first = runner.poll(handle)
        assert RunDir(handle.run_dir).read_meta()["result"]["session_id"] == "sess-abc"
        assert runner.poll(handle) == first

    def test_harvest_reads_already_compressed_stdout(self, tmp_path: Path) -> None:
        runner, handle = self._finished_handle(tmp_path, success_body(tmp_path))
        run_dir = RunDir(handle.run_dir)
        run_dir.compress_logs()  # compressed before any poll harvested it
        result = runner.poll(handle)
        assert result.session_id == "sess-abc"
        assert result.cost == pytest.approx(0.42)

    def test_quota_failure_is_classified_distinctly(self, tmp_path: Path) -> None:
        body = f"echo '{INIT_LINE}'\necho 'Error: Rate Limit reached, retry later' >&2\nexit 1\n"
        runner, handle = self._finished_handle(tmp_path, body)
        result = runner.poll(handle)
        assert result.status is AttemptStatus.EXITED
        assert result.exit_code == 1
        assert result.failure_class is FailureClass.QUOTA
        assert result.session_id == "sess-abc"

    def test_plain_failure_is_error(self, tmp_path: Path) -> None:
        runner, handle = self._finished_handle(tmp_path, "echo 'something broke'\nexit 1\n")
        result = runner.poll(handle)
        assert result.failure_class is FailureClass.ERROR
        assert result.session_id is None
        assert result.cost is None

    def test_running_touches_heartbeat(self, tmp_path: Path) -> None:
        runner = make_runner(make_fake_claude(tmp_path, sleeper_body()))
        node = make_dispatch(tmp_path)
        handle = runner.start(node, make_workspace(tmp_path), ToolPolicy())
        try:
            result = runner.poll(handle)
            assert result.status is AttemptStatus.RUNNING
            assert RunDir(node.run_dir).heartbeat_path.is_file()
        finally:
            runner.cancel(handle)

    def test_timeout_reports_without_killing(self, tmp_path: Path) -> None:
        runner = make_runner(make_fake_claude(tmp_path, sleeper_body()), timeout_seconds=0)
        node = make_dispatch(tmp_path)
        handle = runner.start(node, make_workspace(tmp_path), ToolPolicy())
        try:
            result = runner.poll(handle)
            assert result.status is AttemptStatus.TIMED_OUT
            assert is_alive(handle.pid, handle.create_time)  # cancel is the orchestrator's call
        finally:
            runner.cancel(handle)

    def test_sigkilled_group_synthesizes_crash(self, tmp_path: Path) -> None:
        runner = make_runner(make_fake_claude(tmp_path, sleeper_body()))
        node = make_dispatch(tmp_path)
        handle = runner.start(node, make_workspace(tmp_path), ToolPolicy())
        run_dir = RunDir(node.run_dir)
        wait_for(lambda: run_dir.stdout_path.is_file() and run_dir.stdout_path.read_text())
        os.killpg(os.getpgid(handle.pid), signal.SIGKILL)
        wait_for(lambda: not is_alive(handle.pid, handle.create_time))
        assert run_dir.read_exit_code() is None
        result = runner.poll(handle)
        assert result.status is AttemptStatus.EXITED
        assert result.exit_code == 1
        assert result.failure_class is FailureClass.ERROR
        assert result.session_id == "sess-abc"  # parsed from the partial stream
        assert runner.poll(handle) == result  # cached


class TestResume:
    def _template(self, tmp_path: Path, session_id: str | None) -> RunnerHandle:
        return RunnerHandle(
            node_id="n1",
            attempt=2,
            pid=0,
            create_time=0.0,
            run_dir=tmp_path / "runs/n1/2",
            session_id=session_id,
            workspace=make_workspace(tmp_path),
        )

    def _resume_args(
        self, tmp_path: Path, template: RunnerHandle
    ) -> tuple[RunnerHandle, list[str]]:
        runner = make_runner(make_fake_claude(tmp_path, success_body(tmp_path)))
        handle = runner.resume(template, "fix the tests")
        wait_for(lambda: RunDir(template.run_dir).exit_code_path.is_file())
        return handle, (tmp_path / "args.txt").read_text().splitlines()

    def test_resumes_session_with_feedback(self, tmp_path: Path) -> None:
        template = self._template(tmp_path, session_id="sess-abc")
        handle, args = self._resume_args(tmp_path, template)
        assert args[:2] == ["-p", "fix the tests"]
        assert args[5:] == [
            "--model",
            "cfg-model",
            "--resume",
            "sess-abc",
            "--allowedTools",
            "Read",
        ]
        assert handle.pid > 0
        assert handle.session_id == "sess-abc"  # preserved on the real handle
        assert handle.workspace == template.workspace
        assert RunDir(template.run_dir).prompt_path.read_text() == "fix the tests"
        assert RunDir(template.run_dir).read_meta()["attempt"] == 2

    def test_without_session_falls_back_to_cold_start(self, tmp_path: Path) -> None:
        template = self._template(tmp_path, session_id=None)
        handle, args = self._resume_args(tmp_path, template)
        assert "--resume" not in args
        assert handle.session_id is None

    def test_without_workspace_raises(self, tmp_path: Path) -> None:
        runner = make_runner(make_fake_claude(tmp_path, success_body(tmp_path)))
        template = RunnerHandle(
            node_id="n1", attempt=2, pid=0, create_time=0.0, run_dir=tmp_path / "runs/n1/2"
        )
        with pytest.raises(ValueError, match="workspace"):
            runner.resume(template, "feedback")


class TestCancelAndCapabilities:
    def test_cancel_kills_the_attempt(self, tmp_path: Path) -> None:
        runner = make_runner(make_fake_claude(tmp_path, sleeper_body()))
        node = make_dispatch(tmp_path)
        handle = runner.start(node, make_workspace(tmp_path), ToolPolicy())
        assert is_alive(handle.pid, handle.create_time)
        runner.cancel(handle)
        wait_for(lambda: not is_alive(handle.pid, handle.create_time))

    def test_capabilities(self, tmp_path: Path) -> None:
        runner = make_runner(make_fake_claude(tmp_path, success_body(tmp_path)))
        capabilities = runner.capabilities()
        assert capabilities.supports_resume
        assert capabilities.reports_cost

"""Supervision tests: real detached processes, real git, tmp dirs (ADR-0010)."""

import gzip
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ticketflow.supervision.process import is_alive, kill, spawn_detached
from ticketflow.supervision.run_dirs import RunDir
from ticketflow.supervision.workspace import GitWorkspaces


def wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


class TestSpawnDetached:
    def test_captures_output_and_exit_code(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run")
        spawn_detached(
            ["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=tmp_path, run_dir=run_dir
        )
        code = wait_for(lambda: run_dir.read_exit_code() is not None and run_dir.read_exit_code())
        assert code == 3
        assert run_dir.stdout_path.read_text().strip() == "out"
        assert run_dir.stderr_path.read_text().strip() == "err"

    def test_exit_code_zero_still_signals_completion(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run")
        spawn_detached(["true"], cwd=tmp_path, run_dir=run_dir)
        wait_for(lambda: run_dir.exit_code_path.is_file())
        assert run_dir.read_exit_code() == 0

    def test_survives_process_group_signal(self, tmp_path: Path) -> None:
        # The child is in its own session: killing OUR process group must not
        # touch it. We can't safely signal our own group in a test, so assert
        # the session id differs instead.
        run_dir = RunDir(tmp_path / "run")
        pid, create_time = spawn_detached(["sleep", "30"], cwd=tmp_path, run_dir=run_dir)
        import os

        assert os.getsid(pid) != os.getsid(0)
        assert kill(pid, create_time)


class TestProcessIdentity:
    def test_alive_and_dead(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run")
        pid, create_time = spawn_detached(["sleep", "30"], cwd=tmp_path, run_dir=run_dir)
        assert is_alive(pid, create_time)
        assert kill(pid, create_time)
        wait_for(lambda: not is_alive(pid, create_time))

    def test_wrong_create_time_is_not_the_same_process(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run")
        pid, create_time = spawn_detached(["sleep", "30"], cwd=tmp_path, run_dir=run_dir)
        try:
            assert not is_alive(pid, create_time + 1000.0)
            assert not kill(pid, create_time + 1000.0)  # refuses to signal
        finally:
            kill(pid, create_time)

    def test_nonexistent_pid(self) -> None:
        assert not is_alive(2**22 + 1234, 1.0)


class TestRunDir:
    def test_meta_roundtrip(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run").create()
        run_dir.write_meta({"pid": 1, "runner": "claude", "model": None})
        assert run_dir.read_meta() == {"pid": 1, "runner": "claude", "model": None}

    def test_heartbeat_age(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run").create()
        assert run_dir.heartbeat_age_seconds(datetime.now(UTC)) is None
        run_dir.heartbeat_path.touch()
        age = run_dir.heartbeat_age_seconds(datetime.now(UTC))
        assert age is not None
        assert age < 5

    def test_compress_logs(self, tmp_path: Path) -> None:
        run_dir = RunDir(tmp_path / "run").create()
        run_dir.stdout_path.write_text("hello " * 100)
        compressed = run_dir.compress_logs()
        assert [p.name for p in compressed] == ["stdout.log.gz"]
        assert not run_dir.stdout_path.exists()
        with gzip.open(compressed[0], "rt") as fh:
            assert fh.read().startswith("hello ")


class TestGitWorkspaces:
    def _make_origin(self, tmp_path: Path) -> Path:
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
        seed = tmp_path / "seed"
        subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
        (seed / "README.md").write_text("seed\n")
        env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", *env_args, "add", "."], cwd=seed, check=True)
        subprocess.run(["git", *env_args, "commit", "-m", "seed"], cwd=seed, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=seed, check=True, capture_output=True)
        return origin

    def test_bootstrap_gives_empty_dir(self, tmp_path: Path) -> None:
        ws = GitWorkspaces(tmp_path / "ws", remote_url="unused")
        path = ws.prepare("n1", 1, bootstrap=True)
        assert path.is_dir()
        assert list(path.iterdir()) == []

    def test_worktree_from_default_branch(self, tmp_path: Path) -> None:
        origin = self._make_origin(tmp_path)
        ws = GitWorkspaces(tmp_path / "ws", remote_url=str(origin))
        path = ws.prepare("n1", 1, bootstrap=False)
        assert (path / "README.md").read_text() == "seed\n"
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True
        ).stdout.strip()
        assert branch == "tf/n1"

    def test_prepare_is_idempotent(self, tmp_path: Path) -> None:
        origin = self._make_origin(tmp_path)
        ws = GitWorkspaces(tmp_path / "ws", remote_url=str(origin))
        first = ws.prepare("n1", 1, bootstrap=False)
        again = ws.prepare("n1", 1, bootstrap=False)
        assert first == again

    def test_two_nodes_get_independent_worktrees(self, tmp_path: Path) -> None:
        origin = self._make_origin(tmp_path)
        ws = GitWorkspaces(tmp_path / "ws", remote_url=str(origin))
        a = ws.prepare("a", 1, bootstrap=False)
        b = ws.prepare("b", 1, bootstrap=False)
        assert a != b
        (a / "only-in-a.txt").write_text("x")
        assert not (b / "only-in-a.txt").exists()


class TestWorktreeResume:
    def test_later_attempt_continues_from_pushed_branch(self, tmp_path: Path) -> None:
        origin = TestGitWorkspaces()._make_origin(tmp_path)
        ws = GitWorkspaces(tmp_path / "ws", remote_url=str(origin))
        first = ws.prepare("n1", 1, bootstrap=False)
        (first / "work.txt").write_text("attempt 1")
        env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", *env_args, "add", "."], cwd=first, check=True)
        subprocess.run(["git", *env_args, "commit", "-m", "wip"], cwd=first, check=True)
        subprocess.run(
            ["git", "push", "origin", "tf/n1"], cwd=first, check=True, capture_output=True
        )
        second = ws.prepare("n1", 2, bootstrap=False)
        assert (second / "work.txt").read_text() == "attempt 1"

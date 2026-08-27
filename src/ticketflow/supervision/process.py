"""Detached process spawning and identity (ADR-0010).

The child leaves the orchestrator's process group (its own session), so
orchestrator death never kills an agent. Exit codes are captured by a shell
wrapper writing the ``exit_code`` file; we never wait on the process.

PIDs get reused: identity is always ``(pid, create_time)``, never pid alone.
"""

import os
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import psutil

from ticketflow.supervision.run_dirs import RunDir

_CREATE_TIME_TOLERANCE = 1.0


def spawn_detached(
    command: Sequence[str],
    *,
    cwd: Path,
    run_dir: RunDir,
    env: Mapping[str, str] | None = None,
) -> tuple[int, float]:
    """Start a detached process; returns its ``(pid, create_time)`` identity.

    The command's stdout/stderr stream to the run dir and its exit code lands
    in ``exit_code`` even though nobody waits on it.
    """
    run_dir.create()
    wrapped = (
        f"{shlex.join(command)}"
        f" > {shlex.quote(str(run_dir.stdout_path))}"
        f" 2> {shlex.quote(str(run_dir.stderr_path))}"
        f"; echo $? > {shlex.quote(str(run_dir.exit_code_path))}"
    )
    process = subprocess.Popen(
        ["sh", "-c", wrapped],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # setsid: leaves our process group
    )
    create_time: float = psutil.Process(process.pid).create_time()
    return process.pid, create_time


def is_alive(pid: int, create_time: float) -> bool:
    """True only when the pid exists AND belongs to the same process."""
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - create_time) > _CREATE_TIME_TOLERANCE:
            return False
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def kill(pid: int, create_time: float) -> bool:
    """Kill the process group after validating identity. True if signalled."""
    if not is_alive(pid, create_time):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        return False
    return True

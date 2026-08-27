"""Per-attempt workspaces, detected rather than configured (ADR-0010).

Repo exists: a git worktree branched from the default branch. Repo does not
exist: an empty directory the agent bootstraps. Git operations use the git
CLI via subprocess — pygit2 is excluded by licence policy (ADR-0012).
"""

import subprocess
from pathlib import Path


class GitWorkspaces:
    """WorkspaceProvider backed by one base clone plus worktrees."""

    def __init__(self, root: Path, *, remote_url: str, default_branch: str = "main") -> None:
        self._root = root
        self._remote_url = remote_url
        self._default_branch = default_branch

    @property
    def _base(self) -> Path:
        return self._root / "base"

    def prepare(self, node_id: str, attempt: int, *, bootstrap: bool) -> Path:
        path = self._root / node_id / str(attempt)
        if bootstrap:
            path.mkdir(parents=True, exist_ok=True)
            return path
        if path.is_dir() and (path / ".git").exists():
            return path
        self._ensure_base()
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"tf/{node_id}"
        # One branch can be checked out in one worktree only: retire earlier
        # attempts' worktrees for this node. Their run dirs and logs are
        # untouched — only the superseded checkout goes.
        for sibling in sorted(path.parent.iterdir()):
            if sibling != path and (sibling / ".git").exists():
                self._git("worktree", "remove", "--force", str(sibling), cwd=self._base)
        # A later attempt continues from the node's pushed branch when one
        # exists (the feedback loop); otherwise it starts from the default.
        start_ref = f"origin/{self._default_branch}"
        if self._ref_exists(f"origin/{branch}"):
            start_ref = f"origin/{branch}"
        self._git("worktree", "add", "-B", branch, str(path), start_ref, cwd=self._base)
        return path

    def _ref_exists(self, ref: str) -> bool:
        try:
            self._git("rev-parse", "--verify", "--quiet", ref, cwd=self._base)
        except subprocess.CalledProcessError:
            return False
        return True

    def _ensure_base(self) -> None:
        if (self._base / ".git").exists():
            self._git("fetch", "origin", cwd=self._base)
            return
        self._base.parent.mkdir(parents=True, exist_ok=True)
        self._git("clone", self._remote_url, str(self._base), cwd=self._base.parent)

    @staticmethod
    def _git(*args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

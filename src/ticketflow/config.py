"""Configuration, loaded from a TOML file (ticketflow.toml).

The yolo flag is deliberately NOT part of persisted configuration: it is a
per-run CLI flag, never inherited by a resumed run (ADR-0013).
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TrackerConfig(BaseModel):
    provider: Literal["github", "jira"]
    repo: str | None = None
    """GitHub tracker: owner/repo whose issues are the board."""
    base_url: str | None = None
    """Jira tracker: https://<site>.atlassian.net."""
    project_key: str | None = None
    """Jira tracker: the project whose issues are the board."""
    project_number: int | None = None
    """GitHub tracker: Projects v2 board number for state projection."""
    project_owner: str | None = None
    """GitHub tracker: login owning the Projects v2 board."""


class CodeHostConfig(BaseModel):
    repo: str
    """owner/repo of the target repository."""


class RunnerConfig(BaseModel):
    name: Literal["claude"] = "claude"
    model: str | None = None
    """Model pinned per node class (ADR-0011); None uses the runner default."""
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()


class Limits(BaseModel):
    max_parallel: int = 2
    lease_ttl_seconds: int = 900
    attempt_timeout_seconds: int = 3600
    """Per-attempt wall-clock runaway guard (ADR-0010)."""
    attempt_token_ceiling: int = 1_000_000
    """Per-attempt output-token runaway guard (ADR-0010). Deliberately
    generous: it terminates a stuck loop, it does not manage spend."""
    cycle_cap: int = 100
    """Review-loop backstop, deliberately generous (ADR-0009)."""
    max_attempts: int = 3
    """Dispatch crash retries before escalation ("repeated" in ADR-0006)."""
    halt_ticks: int = 10
    """Halt heuristic N: ticks with nothing dispatchable while escalations
    exist (ADR-0006)."""


class Config(BaseModel):
    tracker: TrackerConfig
    codehost: CodeHostConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    limits: Limits = Field(default_factory=Limits)
    state_dir: Path = Path(".ticketflow")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "ticketflow.db"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def workspaces_dir(self) -> Path:
        return self.state_dir / "workspaces"


def load_config(path: Path) -> Config:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config.model_validate(raw)

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
    issue_type: str = "Task"
    """Jira tracker: issue type for planner-emitted child items (ADR-0014)."""


class CodeHostConfig(BaseModel):
    repo: str
    """owner/repo of the target repository."""


class RunnerConfig(BaseModel):
    name: Literal["claude"] = "claude"
    model: str | None = None
    """Model pinned per node class (ADR-0011); None uses the runner default."""
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()


class PlannerConfig(BaseModel):
    """Offline planner settings (ADR-0014)."""

    synthesis_model: str | None = None
    """Model for the synthesis phase; required to run `plan new` (ADR-0011:
    models come from config, never hardcoded)."""
    grounding_model: str | None = None
    """Model for the grounding agent; None uses the runner default."""
    grounding_allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob")
    """ToolPolicy allowlist for grounding: read-only exploration by default."""
    grounding_disallowed_tools: tuple[str, ...] = ()
    grounding_timeout_seconds: int = 1800
    """Grounding wall-clock runaway guard (ADR-0010): it terminates a stuck
    exploration, it does not manage spend (ADR-0013). The token half of the
    guard is the runner's own ``limits.attempt_token_ceiling``, which
    applies to grounding attempts like any other."""
    synthesis_max_retries: int = 3
    """Schema-or-semantic validation failures retried inside the synthesis
    loop before the turn fails (spec §13.3)."""
    poll_interval_seconds: float = 5.0
    """How often the foreground grounding turn polls the detached attempt."""


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
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    state_dir: Path = Path(".ticketflow")
    plans_dir: Path = Path("plans")
    """Where plan YAML working copies live (ADR-0014): repo-root ``plans/``,
    committed and diffable — the reviewed artifact, unlike run state."""

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

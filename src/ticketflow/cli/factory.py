"""Composition root: build an Orchestrator from config and environment.

Credentials come from the environment, never from the config file:
``GITHUB_TOKEN``/``GH_TOKEN`` for GitHub, ``ATLASSIAN_EMAIL``/``JIRA_EMAIL``
and ``ATLASSIAN_API_TOKEN``/``JIRA_API_TOKEN`` for Jira.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from ticketflow.config import Config
from ticketflow.orchestrator.core import Orchestrator
from ticketflow.ports.runner import RunnerPort
from ticketflow.ports.tracker import TrackerPort
from ticketflow.store.store import Store
from ticketflow.supervision.workspace import GitWorkspaces

if TYPE_CHECKING:
    from ticketflow.planner.service import Planner


def utc_now() -> datetime:
    return datetime.now(UTC)


def github_token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def open_store(config: Config) -> Store:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return Store.open(config.db_path)


def open_store_read_only(config: Config) -> Store:
    """Read-only connection for status views (ADR-0003)."""
    return Store.open_read_only(config.db_path)


def build_tracker(config: Config) -> TrackerPort:
    if config.tracker.provider == "jira":
        from ticketflow.adapters.jira_tracker import JiraTracker

        return JiraTracker(
            config.tracker,
            email=os.environ.get("ATLASSIAN_EMAIL") or os.environ.get("JIRA_EMAIL") or "",
            api_token=os.environ.get("ATLASSIAN_API_TOKEN")
            or os.environ.get("JIRA_API_TOKEN")
            or "",
        )
    from ticketflow.adapters.github_tracker import GitHubTracker

    return GitHubTracker(config.tracker, token=github_token())


def build_runner(
    config: Config, *, yolo: bool = False, clock: Callable[[], datetime] = utc_now
) -> RunnerPort:
    from ticketflow.adapters.claude_runner import ClaudeRunner

    return ClaudeRunner(config.runner, config.limits, clock, yolo=yolo)


def _build_workspaces(config: Config) -> GitWorkspaces:
    return GitWorkspaces(
        config.workspaces_dir,
        remote_url=f"https://github.com/{config.codehost.repo}.git",
    )


def build_orchestrator(
    config: Config,
    store: Store,
    *,
    yolo: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> Orchestrator:
    from ticketflow.adapters.github_codehost import GitHubCodeHost

    return Orchestrator(
        store=store,
        tracker=build_tracker(config),
        runner=build_runner(config, yolo=yolo, clock=clock),
        codehost=GitHubCodeHost(config.codehost.repo, token=github_token()),
        workspaces=_build_workspaces(config),
        config=config,
        clock=clock,
        yolo=yolo,
    )


class _UnconfiguredSynthesizer:
    """Refuses synthesis turns until planner.synthesis_model is configured."""

    def synthesize(self, request: object) -> NoReturn:
        del request
        self._refuse()

    def revise(self, request: object) -> NoReturn:
        del request
        self._refuse()

    @staticmethod
    def _refuse() -> NoReturn:
        from ticketflow.domain.errors import PlanTurnRefused

        raise PlanTurnRefused(
            "planner.synthesis_model is not set — add it under [planner] "
            '(ADR-0011), or set synthesis_backend = "claude-cli" for the CLI default'
        )


def build_planner(
    config: Config,
    store: Store,
    *,
    yolo: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> Planner:
    """Compose the offline planner (ADR-0014).

    The synthesis model must be configured — it is never defaulted
    (ADR-0011); the CLI checks first for a friendlier message.
    """
    from ticketflow.adapters.github_codehost import GitHubCodeHost
    from ticketflow.planner.service import Planner
    from ticketflow.planner.synthesis import PlanSynthesizer
    from ticketflow.planner.validate import semantic_errors

    codehost = GitHubCodeHost(config.codehost.repo, token=github_token())
    synthesizer: PlanSynthesizer
    if config.planner.synthesis_backend == "claude-cli":
        from ticketflow.adapters.claude_cli_synthesis import ClaudeCliSynthesizer

        synthesizer = ClaudeCliSynthesizer(
            validate=semantic_errors,
            model=config.planner.synthesis_model,
            max_retries=config.planner.synthesis_max_retries,
        )
    elif config.planner.synthesis_model is None:
        # Model-free turns (approve, reject, emit, validate, edit) must
        # still work; only a turn that actually synthesizes is refused.
        synthesizer = _UnconfiguredSynthesizer()
    else:
        from ticketflow.adapters.pydanticai_synthesis import PydanticAISynthesizer

        synthesizer = PydanticAISynthesizer(
            model=config.planner.synthesis_model,
            validate=semantic_errors,
            max_retries=config.planner.synthesis_max_retries,
        )
    return Planner(
        store=store,
        tracker=build_tracker(config),
        runner=build_runner(config, yolo=yolo, clock=clock),
        synthesizer=synthesizer,
        workspaces=_build_workspaces(config),
        config=config,
        repo_exists=codehost.repo_exists,
        clock=clock,
        yolo=yolo,
    )

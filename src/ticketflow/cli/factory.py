"""Composition root: build an Orchestrator from config and environment.

Credentials come from the environment, never from the config file:
``GITHUB_TOKEN``/``GH_TOKEN`` for GitHub, ``ATLASSIAN_EMAIL``/``JIRA_EMAIL``
and ``ATLASSIAN_API_TOKEN``/``JIRA_API_TOKEN`` for Jira.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime

from ticketflow.config import Config
from ticketflow.orchestrator.core import Orchestrator
from ticketflow.ports.tracker import TrackerPort
from ticketflow.store.store import Store
from ticketflow.supervision.workspace import GitWorkspaces


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


def build_orchestrator(
    config: Config,
    store: Store,
    *,
    yolo: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> Orchestrator:
    from ticketflow.adapters.claude_runner import ClaudeRunner
    from ticketflow.adapters.github_codehost import GitHubCodeHost

    workspaces = GitWorkspaces(
        config.workspaces_dir,
        remote_url=f"https://github.com/{config.codehost.repo}.git",
    )
    return Orchestrator(
        store=store,
        tracker=build_tracker(config),
        runner=ClaudeRunner(config.runner, config.limits, clock, yolo=yolo),
        codehost=GitHubCodeHost(config.codehost.repo, token=github_token()),
        workspaces=workspaces,
        config=config,
        clock=clock,
        yolo=yolo,
    )

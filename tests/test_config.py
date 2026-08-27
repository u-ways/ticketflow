"""Config loading tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ticketflow.config import Config, load_config


def test_load_minimal_toml(tmp_path: Path) -> None:
    path = tmp_path / "ticketflow.toml"
    path.write_text(
        """
[tracker]
provider = "github"
repo = "u-ways/qa"

[codehost]
repo = "u-ways/qa"
"""
    )
    config = load_config(path)
    assert config.tracker.provider == "github"
    assert config.limits.cycle_cap == 100
    assert config.db_path == Path(".ticketflow/ticketflow.db")


def test_full_toml_overrides(tmp_path: Path) -> None:
    path = tmp_path / "ticketflow.toml"
    path.write_text(
        """
state_dir = "/tmp/tf-state"

[tracker]
provider = "jira"
base_url = "https://x.atlassian.net"
project_key = "PROJ"

[codehost]
repo = "o/r"

[runner]
model = "claude-sonnet-5"
allowed_tools = ["Bash(git:*)", "Edit"]

[limits]
max_parallel = 4
cycle_cap = 5
"""
    )
    config = load_config(path)
    assert config.tracker.project_key == "PROJ"
    assert config.runner.model == "claude-sonnet-5"
    assert config.limits.max_parallel == 4
    assert config.runs_dir == Path("/tmp/tf-state/runs")


def test_invalid_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"tracker": {"provider": "linear"}, "codehost": {"repo": "o/r"}})


def test_yolo_is_not_a_config_field() -> None:
    # ADR-0013: the yolo flag is per-run, never persisted configuration.
    assert "yolo" not in Config.model_fields

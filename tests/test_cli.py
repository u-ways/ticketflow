"""Smoke tests for the CLI entry point."""

from typer.testing import CliRunner

import ticketflow
from ticketflow.cli.app import app

runner = CliRunner()


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert ticketflow.__version__ in result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "ticketflow" in result.output

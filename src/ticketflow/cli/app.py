"""Typer application entry point."""

import typer

import ticketflow

app = typer.Typer(
    name="ticketflow",
    help="Dependency-aware scheduling of coding agents from an issue tracker.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Dependency-aware scheduling of coding agents from an issue tracker."""


@app.command()
def version() -> None:
    """Print the ticketflow version."""
    typer.echo(ticketflow.__version__)


def main() -> None:
    """Console-script entry point."""
    app()

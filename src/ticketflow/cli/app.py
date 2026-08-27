"""Typer application entry point.

Every human action here writes an intent (ADR-0004) — the CLI is not
privileged and never mutates node state directly. Status commands are
read-only projections of the store (ADR-0003).
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

import ticketflow
from ticketflow.config import Config, load_config
from ticketflow.domain.model import NodeState

if TYPE_CHECKING:
    from ticketflow.store.store import Store

app = typer.Typer(
    name="ticketflow",
    help="Dependency-aware scheduling of coding agents from an issue tracker.",
    no_args_is_help=True,
)

ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="Path to ticketflow.toml.")]
DEFAULT_CONFIG = Path("ticketflow.toml")

STARTER_CONFIG = """\
[tracker]
provider = "github"          # or "jira"
repo = "owner/repo"          # GitHub Issues board (github provider)
# base_url = "https://your-site.atlassian.net"   # jira provider
# project_key = "PROJ"                            # jira provider
# project_owner = "owner"    # optional: GitHub Projects v2 board for state
# project_number = 1

[codehost]
repo = "owner/repo"          # the target repository for branches and PRs

[runner]
name = "claude"
# model = "claude-sonnet-5"  # pinned per node class; omit for the CLI default
allowed_tools = []           # ToolPolicy allowlist compiled to CLI flags
disallowed_tools = []

[limits]
max_parallel = 2
lease_ttl_seconds = 900
attempt_timeout_seconds = 3600
cycle_cap = 100
max_attempts = 3
halt_ticks = 10
"""


def _open_read_only(config_path: Path) -> Store | None:
    """Open the state store read-only (ADR-0003); None when no state exists."""
    import sqlite3

    from ticketflow.cli.factory import open_store_read_only

    cfg = _load(config_path)
    if not cfg.db_path.is_file():
        typer.echo("No state yet: the orchestrator has not run.")
        return None
    try:
        return open_store_read_only(cfg)
    except sqlite3.OperationalError as exc:
        typer.echo(f"Cannot open state store: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _load(config_path: Path) -> Config:
    if not config_path.is_file():
        typer.echo(f"No config at {config_path}. Run `ticketflow init` first.", err=True)
        raise typer.Exit(code=2)
    return load_config(config_path)


@app.callback()
def _root() -> None:
    """Dependency-aware scheduling of coding agents from an issue tracker."""


@app.command()
def version() -> None:
    """Print the ticketflow version."""
    typer.echo(ticketflow.__version__)


@app.command()
def init(path: Annotated[Path, typer.Argument()] = DEFAULT_CONFIG) -> None:
    """Write a starter ticketflow.toml."""
    if path.exists():
        typer.echo(f"{path} already exists; not overwriting.", err=True)
        raise typer.Exit(code=1)
    path.write_text(STARTER_CONFIG, encoding="utf-8")
    typer.echo(f"Wrote {path}. Edit it, then run `ticketflow run`.")


@app.command()
def run(
    config: ConfigOption = DEFAULT_CONFIG,
    yolo: Annotated[
        bool, typer.Option("--yolo", help="Auto-approve plans; no tool permission prompts.")
    ] = False,
    once: Annotated[bool, typer.Option("--once", help="Run a single tick and exit.")] = False,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between ticks.")] = 15.0,
) -> None:
    """Run the orchestrator loop: adopt in-flight work, then tick."""
    from ticketflow.cli.factory import build_orchestrator, open_store

    cfg = _load(config)
    if yolo:
        # One warning at startup, then nothing (ADR-0013).
        typer.echo(
            "yolo: plans auto-approve and agents run without permission prompts. "
            "The repo's own gates are now the only thing checking the work.",
            err=True,
        )
    store = open_store(cfg)
    try:
        orchestrator = build_orchestrator(cfg, store, yolo=yolo)
        orchestrator.adopt()
        while True:
            report = orchestrator.tick()
            typer.echo(
                f"tick: synced={report.synced} intents={report.intents_processed} "
                f"dispatched={report.dispatched} settled={report.settled} "
                f"merged={report.merged} escalated={report.escalated}"
                + (f" notes={'; '.join(report.notes)}" if report.notes else "")
            )
            if report.halted:
                typer.echo(
                    "halted: nothing dispatchable while escalations exist. "
                    "See `ticketflow escalations`.",
                    err=True,
                )
                raise typer.Exit(code=3)
            if once:
                return
            time.sleep(interval)
    finally:
        store.close()


@app.command()
def status(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Show every node and its state (read-only)."""
    store = _open_read_only(config)
    if store is None:
        return
    try:
        nodes = store.list_nodes()
        if not nodes:
            typer.echo("No nodes synced yet.")
            return
        for node in nodes:
            refs = ", ".join(ref.external_key for ref in store.refs_for(node.node_id))
            reason = f"  [{node.blocked_reason}]" if node.blocked_reason else ""
            typer.echo(
                f"{node.node_id}  {node.state.value:<20} {refs:<12} "
                f"attempts={node.attempt_count} cycles={node.cycle_count}  "
                f"{node.title}{reason}"
            )
    finally:
        store.close()


@app.command()
def escalations(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """List escalated nodes and why they need a human (read-only)."""
    store = _open_read_only(config)
    if store is None:
        return
    try:
        nodes = store.list_nodes(state=NodeState.ESCALATED)
        if not nodes:
            typer.echo("No escalations.")
            return
        for node in nodes:
            typer.echo(f"{node.node_id}  {node.title}\n    reason: {node.blocked_reason}")
            typer.echo(
                f"    resolve: ticketflow retry {node.node_id} [--feedback '...'] "
                f"| ticketflow cancel {node.node_id}"
            )
    finally:
        store.close()


@app.command()
def events(
    config: ConfigOption = DEFAULT_CONFIG,
    after: Annotated[int, typer.Option("--after", help="Only events after this id.")] = 0,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """Tail the append-only event log (read-only)."""
    store = _open_read_only(config)
    if store is None:
        return
    try:
        for event in store.events_after(after, limit=limit):
            where = f" node={event.node_id}" if event.node_id else ""
            attempt = f" attempt={event.attempt}" if event.attempt else ""
            typer.echo(
                f"{event.event_id:>6}  {event.ts.isoformat()}  {event.kind}"
                f"{where}{attempt}  {event.payload}"
            )
    finally:
        store.close()


def _write_intent(
    config_path: Path, intent_type: str, node_id: str | None, payload: dict[str, str]
) -> None:
    from ticketflow.cli.factory import open_store, utc_now

    store = open_store(_load(config_path))
    try:
        intent_id = store.add_intent(
            intent_type=intent_type,
            source="cli",
            node_id=node_id,
            payload=payload,
            now=utc_now(),
        )
        typer.echo(
            f"intent {intent_id} recorded ({intent_type}"
            + (f" {node_id}" if node_id else "")
            + "); the orchestrator applies it on its next tick."
        )
    finally:
        store.close()


@app.command()
def retry(
    node_id: str,
    config: ConfigOption = DEFAULT_CONFIG,
    feedback: Annotated[
        str, typer.Option("--feedback", help="Correction passed to the agent's next attempt.")
    ] = "",
) -> None:
    """Re-enter an escalated node at Ready (writes a retry intent)."""
    _write_intent(config, "retry", node_id, {"feedback": feedback} if feedback else {})


@app.command()
def cancel(node_id: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Cancel a node (writes a cancel intent)."""
    _write_intent(config, "cancel", node_id, {})


@app.command()
def unblock(node_id: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Force a blocked node to Ready (writes an unblock intent)."""
    _write_intent(config, "unblock", node_id, {})


@app.command()
def resume(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Resume dispatch after a quota pause (writes a global resume intent)."""
    _write_intent(config, "resume", None, {})


def main() -> None:
    """Console-script entry point."""
    app()

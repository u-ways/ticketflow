"""Typer application entry point.

Every human action here writes an intent (ADR-0004) — the CLI is not
privileged and never mutates node state directly. Status commands are
read-only projections of the store (ADR-0003). The ``plan`` sub-app is the
offline planner's surface (ADR-0014): planner turns are the second scoped
store writer of ADR-0003's revision (plan* tables, ``plan_*`` intent
``processed_at``, events — nothing else).
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

import ticketflow
from ticketflow.config import Config, load_config
from ticketflow.domain.model import NodeState

if TYPE_CHECKING:
    from ticketflow.planner.service import Planner
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

# [planner]                    # offline planner (ADR-0014): `ticketflow plan`
# synthesis_model = "claude-sonnet-5"  # required for `plan new`; never defaulted
# grounding_model = "claude-sonnet-5"  # omit for the runner default
# grounding_allowed_tools = ["Read", "Grep", "Glob"]
# grounding_timeout_seconds = 1800     # runaway guard, not a budget
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
        typer.echo(_YOLO_WARNING, err=True)
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


# -- the offline planner (ADR-0014) -----------------------------------------

plan_app = typer.Typer(
    name="plan",
    help="Offline planner (ADR-0014): propose a decomposition, review it, emit it.",
    no_args_is_help=True,
)
app.add_typer(plan_app)

_YOLO_WARNING = (
    "yolo: plans auto-approve and agents run without permission prompts. "
    "The repo's own gates are now the only thing checking the work."
)


def _build_planner_or_exit(
    config_path: Path, *, yolo: bool = False
) -> tuple[Config, Store, Planner]:
    from ticketflow.cli.factory import build_planner, open_store

    cfg = _load(config_path)
    if cfg.planner.synthesis_backend == "pydantic-ai" and cfg.planner.synthesis_model is None:
        typer.echo(
            "planner.synthesis_model is not set. Add it under [planner] in "
            f"{config_path} — the model always comes from config (ADR-0011) — "
            'or set synthesis_backend = "claude-cli" to use the CLI default.',
            err=True,
        )
        raise typer.Exit(code=2)
    store = open_store(cfg)
    return cfg, store, build_planner(cfg, store, yolo=yolo)


@contextmanager
def _plan_errors() -> Iterator[None]:
    from ticketflow.domain.errors import (
        GroundingFailed,
        PlanTurnRefused,
        PlanValidationError,
        UnknownEpic,
    )

    try:
        yield
    except (UnknownEpic, PlanTurnRefused, PlanValidationError, GroundingFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _plan_file(cfg: Config, epic_key: str) -> Path:
    from ticketflow.planner.yaml_io import plan_filename

    return cfg.plans_dir / plan_filename(epic_key)


@plan_app.command("new")
def plan_new(
    epic_key: str,
    config: ConfigOption = DEFAULT_CONFIG,
    yolo: Annotated[
        bool, typer.Option("--yolo", help="Auto-approve the plan and emit it immediately.")
    ] = False,
) -> None:
    """Ground the epic, synthesize a plan, and surface it for review.

    Resumable: re-running continues from wherever a previous run stopped.
    """
    from ticketflow.domain.plan import PlanStatus

    if yolo:
        # One warning at startup, then nothing (ADR-0013).
        typer.echo(_YOLO_WARNING, err=True)
    cfg, store, planner = _build_planner_or_exit(config, yolo=yolo)
    try:
        with _plan_errors():
            plan = planner.new(epic_key)
        typer.echo(
            f"plan {plan.plan_id} for {epic_key}: {plan.status.value} "
            f"(revision {plan.current_revision})"
        )
        if plan.status is PlanStatus.IN_REVIEW:
            typer.echo(
                f"review it: `ticketflow plan show {epic_key}`, hand-edit "
                f"{_plan_file(cfg, epic_key)} (then `ticketflow plan validate`), or "
                f"`ticketflow plan revise {epic_key} --feedback '...'`; approve with "
                f"`ticketflow plan approve {epic_key}` and emit with "
                f"`ticketflow plan emit {epic_key}`."
            )
    finally:
        store.close()


@plan_app.command("show")
def plan_show(epic_key: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Show the plan: items, then edges ascending by confidence (read-only).

    Review is mostly pruning (spec §13.2), so the least-evidenced proposals
    come first and uncited ones are listed apart.
    """
    from ticketflow.planner.yaml_io import load_plan

    cfg = _load(config)
    store = _open_read_only(config)
    if store is None:
        return
    try:
        plan = store.plan_for_epic(cfg.tracker.provider, epic_key)
        if plan is None:
            typer.echo(f"No live plan for {epic_key}.")
            return
        typer.echo(
            f"{plan.plan_id}  {plan.status.value:<12} revision={plan.current_revision}  "
            f"epic={plan.epic_key}"
        )
        blob = store.get_plan_revision(plan.plan_id, plan.current_revision)
        if blob is not None:
            parsed = load_plan(blob.yaml)
            typer.echo("items:")
            for item in parsed.items:
                typer.echo(f"  [{item.index}] {item.title}")
            if parsed.edges:
                typer.echo("edges, ascending by confidence (prune what you don't believe):")
                for edge in sorted(parsed.edges, key=lambda e: e.confidence):
                    typer.echo(
                        f"  {edge.upstream} -> {edge.downstream}  "
                        f"{edge.confidence:.2f}  {edge.evidence}"
                    )
            if parsed.unevidenced_edges:
                typer.echo("proposed WITHOUT evidence (promote with evidence, or delete):")
                for edge in parsed.unevidenced_edges:
                    typer.echo(f"  {edge.upstream} -> {edge.downstream}  {edge.confidence:.2f}")
        emitted = store.emitted_items(plan.plan_id)
        if emitted:
            typer.echo("emission:")
            for entry in emitted:
                edges = "edges written" if entry.edges_written_at else "edges pending"
                typer.echo(f"  [{entry.item_index}] {entry.external_key}  {edges}")
    finally:
        store.close()


@plan_app.command("list")
def plan_list(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """List every plan and its lifecycle status (read-only)."""
    store = _open_read_only(config)
    if store is None:
        return
    try:
        plans = store.list_plans()
        if not plans:
            typer.echo("No plans yet. Start one with `ticketflow plan new <epic-key>`.")
            return
        for plan in plans:
            typer.echo(
                f"{plan.plan_id}  {plan.status.value:<12} revision={plan.current_revision}  "
                f"{plan.epic_key}"
            )
    finally:
        store.close()


@plan_app.command("edit")
def plan_edit(epic_key: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Open the plan YAML in $EDITOR, then validate the edit as a revision.

    A failing edit rejects the turn, not the plan: the file stays on disk
    for fixing and the previous revision stands (spec §13.5).
    """
    import os
    import shlex
    import subprocess

    cfg, store, planner = _build_planner_or_exit(config)
    try:
        with _plan_errors():
            path = _plan_file(cfg, epic_key)
            if not path.is_file():
                from ticketflow.planner.yaml_io import write_plan_file

                plan = planner.ingest(epic_key)
                blob = store.get_plan_revision(plan.plan_id, plan.current_revision)
                if blob is None:
                    typer.echo(f"nothing to edit yet: run `ticketflow plan new {epic_key}`.")
                    return
                write_plan_file(cfg.plans_dir, epic_key, blob.yaml)
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
            subprocess.run([*shlex.split(editor), str(path)], check=True)
            revision = planner.validate_file(epic_key)
        if revision is None:
            typer.echo("no changes.")
        else:
            typer.echo(f"revision {revision} recorded from your edit.")
    finally:
        store.close()


@plan_app.command("validate")
def plan_validate(epic_key: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Validate a hand-edited plan file and record it as a revision."""
    _, store, planner = _build_planner_or_exit(config)
    try:
        with _plan_errors():
            revision = planner.validate_file(epic_key)
        if revision is None:
            typer.echo("plan file matches the current revision; nothing recorded.")
        else:
            typer.echo(f"revision {revision} recorded from your edit.")
    finally:
        store.close()


@plan_app.command("revise")
def plan_revise(
    epic_key: str,
    feedback: Annotated[
        str, typer.Option("--feedback", help="What to change and why, in your words.")
    ],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """One conversational revision turn: the planner applies your feedback
    and re-derives its consequences; the result is validated and versioned."""
    _, store, planner = _build_planner_or_exit(config)
    try:
        with _plan_errors():
            revision = planner.revise(epic_key, feedback)
        typer.echo(f"revision {revision} recorded. Review with `ticketflow plan show`.")
    finally:
        store.close()


@plan_app.command("approve")
def plan_approve(
    epic_key: str,
    config: ConfigOption = DEFAULT_CONFIG,
    rev: Annotated[
        int | None,
        typer.Option(
            "--rev",
            help="Revision to approve; must be the latest (approval is digest-pinned).",
        ),
    ] = None,
) -> None:
    """Approve the plan (writes a plan_approve intent, ADR-0004).

    All-or-nothing: the whole revision is approved, pinned by content
    digest. Emission happens on `ticketflow plan emit`.
    """
    _, store, planner = _build_planner_or_exit(config)
    try:
        with _plan_errors():
            intent_id = planner.request_approval(epic_key, rev)
        if intent_id is None:
            typer.echo("that approval is already recorded.")
        else:
            typer.echo(
                f"intent {intent_id} recorded (plan_approve); "
                f"run `ticketflow plan emit {epic_key}` to create the child tickets."
            )
    finally:
        store.close()


@plan_app.command("reject")
def plan_reject(
    epic_key: str,
    reason: Annotated[str, typer.Option("--reason", help="Why the plan is wrong.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Reject the plan: a plan_reject intent is written and consumed in the
    same turn, discarding the plan (ADR-0004)."""
    _, store, planner = _build_planner_or_exit(config)
    try:
        with _plan_errors():
            intent_id = planner.request_rejection(epic_key, reason)
        if intent_id is None:
            typer.echo("that rejection is already recorded.")
        else:
            typer.echo(
                f"intent {intent_id} recorded (plan_reject); the plan is discarded. "
                f"`ticketflow plan new {epic_key}` starts a fresh one."
            )
    finally:
        store.close()


@plan_app.command("emit")
def plan_emit(epic_key: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Emit the approved plan as tracker tickets. Idempotent and resumable:
    re-running after a failure adopts what already exists (ADR-0014)."""
    _, store, planner = _build_planner_or_exit(config)
    try:
        with _plan_errors():
            report = planner.emit(epic_key)
        if report.complete:
            listed = ", ".join(report.child_keys)
            typer.echo(
                f"emitted {len(report.child_keys)} child items ({listed}); "
                f"created={report.created} adopted={report.adopted} "
                f"mirrored={report.mirrored}. The orchestrator picks them up on sync."
            )
        else:
            typer.echo(
                f"emission incomplete: {report.failure}\n"
                f"created so far: {', '.join(report.child_keys) or '(none)'}\n"
                f"re-run `ticketflow plan emit {epic_key}` to resume; nothing is rolled back.",
                err=True,
            )
            raise typer.Exit(code=3)
    finally:
        store.close()


def main() -> None:
    """Console-script entry point."""
    app()

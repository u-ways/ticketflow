"""Idempotent, resumable plan emission (ADR-0014).

The approved revision blob — never the working-copy file — is what gets
emitted. All items are created before any edge is written; every step is
memoised in the ``plan_emitted_items`` ledger whose primary key is the
(plan id, item index) idempotency key; an adoption sweep before each run
closes the created-but-unrecorded crash window by reading the ``tf-plan:``
marker back off the tracker. There is deliberately NO delete or rollback
path anywhere in this module: on permanent failure the partials stay,
tagged and invisible to the scheduler, and re-running emit is the recovery.

Both evidenced and unevidenced edges are emitted — pruning happened in
review (spec §13.2, ADR-0014).

Dependency mirrors (phase 3) are cosmetic and best-effort: a mirror failure
is evented and never blocks completion (ADR-0007).
"""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ticketflow.domain.parser import parse_body, render_child_body
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.planner.schema import Plan
from ticketflow.planner.validate import semantic_errors
from ticketflow.planner.yaml_io import load_plan
from ticketflow.ports.tracker import TrackerPort
from ticketflow.store.store import Store


def plan_label(plan_id: str) -> str:
    """Tracker label tagging every emitted child (no spaces: Jira-safe)."""
    return f"tf-plan-{plan_id}"


@dataclass
class EmitReport:
    created: int = 0
    adopted: int = 0
    edges_written: int = 0
    mirrored: int = 0
    complete: bool = False
    failure: str | None = None
    child_keys: list[str] = field(default_factory=list)


def run_emit(
    store: Store,
    tracker: TrackerPort,
    plan: PlanRecord,
    *,
    clock: Callable[[], datetime],
) -> EmitReport:
    """Emit the approved plan; safe to re-run at any point, from any crash."""
    report = EmitReport()
    if plan.status is not PlanStatus.EMITTING or plan.approved_revision is None:
        report.failure = f"plan is {plan.status.value}, not emitting"
        return report
    blob = store.get_plan_revision(plan.plan_id, plan.approved_revision)
    assert blob is not None  # approve_plan pinned an existing revision
    approved = load_plan(blob.yaml)
    errors = semantic_errors(approved)
    if errors:  # defence in depth; approval validated already
        report.failure = "approved plan failed validation: " + "; ".join(errors)
        return report

    try:
        _adoption_sweep(store, tracker, plan, report, clock)
        _create_items(store, tracker, plan, approved, report, clock)
        _write_edges(store, tracker, plan, approved, report, clock)
        _mirror(store, tracker, plan, approved, report, clock)
    except Exception as exc:
        # evented, surfaced on the epic, resumed by re-running emit (ADR-0014).
        return _failed(store, tracker, plan, report, str(exc), clock)

    ledger = {entry.item_index: entry for entry in store.emitted_items(plan.plan_id)}
    done = all(
        item.index in ledger and ledger[item.index].edges_written_at is not None
        for item in approved.items
    )
    if not done:
        return _failed(store, tracker, plan, report, "emission incomplete", clock)

    report.child_keys = [ledger[item.index].external_key for item in approved.items]
    now = clock()
    store.set_plan_status(plan.plan_id, PlanStatus.EMITTED, now=now)
    store.append_event(
        "plan_emitted",
        now=now,
        payload={"plan_id": plan.plan_id, "children": report.child_keys},
    )
    with contextlib.suppress(Exception):
        listed = "\n".join(f"- {key}" for key in report.child_keys)
        tracker.push_comment(
            plan.epic_key,
            f"ticketflow emitted this epic's plan as {len(report.child_keys)} "
            f"child items:\n{listed}",
        )
    report.complete = True
    return report


def _adoption_sweep(
    store: Store,
    tracker: TrackerPort,
    plan: PlanRecord,
    report: EmitReport,
    clock: Callable[[], datetime],
) -> None:
    """Adopt tracker items carrying this plan's marker but missing from the
    ledger — the crash window between create_item and its ledger row."""
    ledger = {entry.item_index: entry for entry in store.emitted_items(plan.plan_id)}
    items, _ = tracker.fetch_nodes(None)
    for item in items:
        if item.closed:
            # Closing a ticket is the operator's dedup/junk action (the
            # anomaly message asks for exactly that): a closed item is
            # neither adopted nor a claimant.
            continue
        marker = parse_body(item.body).plan_marker
        if marker is None or marker[0] != plan.plan_id:
            continue
        index = marker[1]
        known = ledger.get(index)
        if known is None:
            store.record_emitted_item(
                plan.plan_id, index, external_key=item.external_key, now=clock()
            )
            store.append_event(
                "plan_item_adopted",
                now=clock(),
                payload={
                    "plan_id": plan.plan_id,
                    "item_index": index,
                    "external_key": item.external_key,
                },
            )
            report.adopted += 1
        elif known.external_key != item.external_key:
            store.append_event(
                "plan_emit_anomaly",
                now=clock(),
                payload={
                    "plan_id": plan.plan_id,
                    "item_index": index,
                    "ledger_key": known.external_key,
                    "tracker_key": item.external_key,
                },
            )
            raise RuntimeError(
                f"two tracker items claim plan item {index}: "
                f"{known.external_key} and {item.external_key}"
            )


def _create_items(
    store: Store,
    tracker: TrackerPort,
    plan: PlanRecord,
    approved: Plan,
    report: EmitReport,
    clock: Callable[[], datetime],
) -> None:
    """Phase 1: every item exists before any edge is written (ADR-0014)."""
    ledger = {entry.item_index for entry in store.emitted_items(plan.plan_id)}
    for item in sorted(approved.items, key=lambda i: i.index):
        if item.index in ledger:
            continue
        body = render_child_body(
            item.body, plan_id=plan.plan_id, item_index=item.index, scope=item.scope
        )
        parsed = parse_body(body)
        if parsed.plan_marker != (plan.plan_id, item.index) or parsed.issues:
            raise RuntimeError(f"rendered body for item {item.index} failed the round-trip check")
        key = tracker.create_item(
            item.title, body, labels=(plan_label(plan.plan_id),), parent_key=plan.epic_key
        )
        if not store.record_emitted_item(plan.plan_id, item.index, external_key=key, now=clock()):
            # A ledger row appeared between our snapshot and this insert:
            # a concurrent emit turn created the item first, and OUR ticket
            # is now a duplicate. Concurrent emits are unsupported — fail
            # loudly and name the ticket the operator must close.
            store.append_event(
                "plan_emit_anomaly",
                now=clock(),
                payload={
                    "plan_id": plan.plan_id,
                    "item_index": item.index,
                    "duplicate_key": key,
                },
            )
            raise RuntimeError(
                f"concurrent emit detected on item {item.index}: {key} is a "
                "duplicate ticket — close it, then re-run emit"
            )
        store.append_event(
            "plan_item_created",
            now=clock(),
            payload={"plan_id": plan.plan_id, "item_index": item.index, "external_key": key},
        )
        report.created += 1


def _upstreams_of(approved: Plan, index: int) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                edge.upstream
                for edge in (*approved.edges, *approved.unevidenced_edges)
                if edge.downstream == index
            }
        )
    )


def _write_edges(
    store: Store,
    tracker: TrackerPort,
    plan: PlanRecord,
    approved: Plan,
    report: EmitReport,
    clock: Callable[[], datetime],
) -> None:
    """Phase 2: rewrite each dependent item's body with its depends-on line,
    round-trip-checked so a malformed block can never be pushed (ADR-0007)."""
    ledger = {entry.item_index: entry for entry in store.emitted_items(plan.plan_id)}
    for item in sorted(approved.items, key=lambda i: i.index):
        entry = ledger[item.index]
        if entry.edges_written_at is not None:
            continue
        # Every body is rewritten from the approved revision — including
        # no-upstream items, so an adopted orphan's crash-window edits are
        # normalized like everyone else's (ADR-0014).
        upstreams = _upstreams_of(approved, item.index)
        keys = tuple(ledger[upstream].external_key for upstream in upstreams)
        body = render_child_body(
            item.body,
            plan_id=plan.plan_id,
            item_index=item.index,
            depends_on=keys,
            scope=item.scope,
        )
        parsed = parse_body(body)
        if parsed.depends_on != keys or parsed.plan_marker != (plan.plan_id, item.index):
            raise RuntimeError(f"rendered body for item {item.index} failed the round-trip check")
        tracker.update_body(entry.external_key, body)
        store.mark_item_edges_written(plan.plan_id, item.index, now=clock())
        store.append_event(
            "plan_edges_written",
            now=clock(),
            payload={"plan_id": plan.plan_id, "item_index": item.index, "depends_on": list(keys)},
        )
        report.edges_written += 1


def _mirror(
    store: Store,
    tracker: TrackerPort,
    plan: PlanRecord,
    approved: Plan,
    report: EmitReport,
    clock: Callable[[], datetime],
) -> None:
    """Phase 3: best-effort native dependency mirrors (ADR-0007).

    Failures are evented and never block emission — the body is the truth
    and the mirror is cosmetic."""
    ledger = {entry.item_index: entry for entry in store.emitted_items(plan.plan_id)}
    for item in sorted(approved.items, key=lambda i: i.index):
        entry = ledger.get(item.index)
        if entry is None or entry.mirrored_at is not None:
            continue
        upstreams = _upstreams_of(approved, item.index)
        if not upstreams:
            continue
        keys = tuple(ledger[upstream].external_key for upstream in upstreams)
        try:
            tracker.mirror_dependencies(entry.external_key, keys)
        except Exception as exc:
            store.append_event(
                "plan_mirror_failed",
                now=clock(),
                payload={
                    "plan_id": plan.plan_id,
                    "item_index": item.index,
                    "error": str(exc),
                },
            )
            continue
        store.mark_item_mirrored(plan.plan_id, item.index, now=clock())
        report.mirrored += 1


def _failed(
    store: Store,
    tracker: TrackerPort,
    plan: PlanRecord,
    report: EmitReport,
    error: str,
    clock: Callable[[], datetime],
) -> EmitReport:
    """Permanent-failure surface: event + epic comment, partials left in
    place, status stays ``emitting`` — re-running emit is the recovery."""
    entries = store.emitted_items(plan.plan_id)
    created = [entry.external_key for entry in entries]
    now = clock()
    store.append_event(
        "plan_emit_failed",
        now=now,
        payload={"plan_id": plan.plan_id, "created": created, "error": error},
    )
    with contextlib.suppress(Exception):
        listed = "\n".join(f"- {key}" for key in created) or "- (none)"
        tracker.push_comment(
            plan.epic_key,
            "ticketflow could not finish emitting this epic's plan.\n"
            f"Error: {error}\nChildren that already exist (tagged "
            f"`{plan_label(plan.plan_id)}`, held until emission completes):\n{listed}\n"
            "Re-running `ticketflow plan emit` resumes without duplicating them.",
        )
    report.failure = error
    report.child_keys = created
    return report

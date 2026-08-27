"""Plan approval consumption (ADR-0004, ADR-0014).

Approval and rejection arrive as ``plan_approve``/``plan_reject`` intent
rows, written by the CLI (or by ``--yolo`` writing the same row). The
orchestrator's tick leaves the whole ``plan_`` namespace pending; the
planner turn consumes it here, under the same ``processed_at`` guard, so a
crashed turn re-runs as a no-op. Approval pins one revision by number AND
content digest — a file edited after approval refuses to emit until
re-approved — and the ``plan_approved`` event carries the
first-proposal-versus-approved diff (spec §13.5 rule 3).
"""

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ticketflow.domain.model import Intent
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.planner.schema import Plan
from ticketflow.planner.yaml_io import load_plan
from ticketflow.store.store import Store

PLAN_INTENT_PREFIX = "plan_"
"""Intent-type namespace the orchestrator tick must leave pending (the
``_consume_intents`` skip cross-references this constant)."""


def yaml_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def consume_plan_intents(
    store: Store, plan: PlanRecord, *, clock: Callable[[], datetime]
) -> PlanRecord:
    """Apply this plan's pending approval/rejection intents, idempotently.

    Every branch marks the intent processed (ADR-0004: the only intent
    mutation is ``processed_at``); re-running is a no-op. Returns the
    refreshed plan record.
    """
    for intent in store.unprocessed_intents():
        if not intent.intent_type.startswith(PLAN_INTENT_PREFIX):
            continue
        if intent.payload.get("plan_id") != plan.plan_id:
            continue
        now = clock()
        if intent.intent_type == "plan_approve":
            _apply_approval(store, plan, intent, now)
        elif intent.intent_type == "plan_reject":
            _apply_rejection(store, plan, intent, now)
        else:
            store.append_event(
                "plan_intent_ignored",
                now=now,
                payload={"plan_id": plan.plan_id, "type": intent.intent_type},
            )
        store.mark_intent_processed(intent.intent_id, now=now)
        refreshed = store.get_plan(plan.plan_id)
        assert refreshed is not None
        plan = refreshed
    return plan


def first_vs_approved_diff(first: Plan, approved: Plan) -> dict[str, Any]:
    """The labelled dataset of spec §13.5 rule 3: how review changed the
    proposal. Edges removed carry their proposed confidence — that is what
    calibrates the §13.2 thresholds."""
    first_titles = {item.index: item.title for item in first.items}
    approved_titles = {item.index: item.title for item in approved.items}
    first_edges = {(e.upstream, e.downstream): e for e in (*first.edges, *first.unevidenced_edges)}
    approved_edges = {
        (e.upstream, e.downstream): e for e in (*approved.edges, *approved.unevidenced_edges)
    }
    return {
        "items_added": sorted(set(approved_titles) - set(first_titles)),
        "items_removed": sorted(set(first_titles) - set(approved_titles)),
        "items_retitled": sorted(
            index
            for index in set(first_titles) & set(approved_titles)
            if first_titles[index] != approved_titles[index]
        ),
        "edges_added": [
            {
                "upstream": up,
                "downstream": down,
                "confidence": approved_edges[(up, down)].confidence,
            }
            for (up, down) in sorted(set(approved_edges) - set(first_edges))
        ],
        "edges_removed": [
            {"upstream": up, "downstream": down, "confidence": first_edges[(up, down)].confidence}
            for (up, down) in sorted(set(first_edges) - set(approved_edges))
        ],
    }


def _apply_approval(store: Store, plan: PlanRecord, intent: Intent, now: datetime) -> None:
    if plan.status is PlanStatus.EMITTING:
        return  # already approved; emit resumes regardless
    if plan.status is not PlanStatus.IN_REVIEW:
        store.append_event(
            "plan_intent_ignored",
            now=now,
            payload={"plan_id": plan.plan_id, "type": "plan_approve", "status": plan.status.value},
        )
        return

    revision = intent.payload.get("revision")
    blob = store.get_plan_revision(plan.plan_id, revision) if isinstance(revision, int) else None
    stale_reason = None
    if blob is None or revision != plan.current_revision:
        stale_reason = f"approved revision {revision}, current is {plan.current_revision}"
    elif intent.payload.get("yaml_sha256") != yaml_sha256(blob.yaml):
        stale_reason = "approved content no longer matches the stored revision"
    if stale_reason is not None:
        # Refuse rather than emit something nobody read: re-validate and
        # re-approve is the operator's next step.
        store.append_event(
            "plan_approval_stale",
            now=now,
            payload={"plan_id": plan.plan_id, "revision": revision, "reason": stale_reason},
        )
        return

    assert blob is not None
    first = store.get_plan_revision(plan.plan_id, 1)
    assert first is not None  # revision numbering starts at 1
    diff = first_vs_approved_diff(load_plan(first.yaml), load_plan(blob.yaml))
    store.approve_plan(plan.plan_id, blob.revision, now=now, diff=diff)


def _apply_rejection(store: Store, plan: PlanRecord, intent: Intent, now: datetime) -> None:
    retractable = plan.status is PlanStatus.EMITTING and not store.emitted_items(plan.plan_id)
    if plan.status in (PlanStatus.EMITTED, PlanStatus.DISCARDED) or (
        plan.status is PlanStatus.EMITTING and not retractable
    ):
        store.append_event(
            "plan_intent_ignored",
            now=now,
            payload={"plan_id": plan.plan_id, "type": "plan_reject", "status": plan.status.value},
        )
        return
    reason = str(intent.payload.get("reason") or f"rejected by {intent.source}")
    store.set_plan_status(plan.plan_id, PlanStatus.DISCARDED, now=now, reason=reason)

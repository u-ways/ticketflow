"""The reconcile tick (ADR-0008).

A fixed, ordered, side-effect-explicit sequence:

    intents -> tracker sync -> reconcile attempts -> settle PRs
            -> ready-set -> dispatch -> board projection

Each step is deterministic Python, unit-tested against fakes. No model is
ever consulted here; agents run inside nodes and never decide what runs next.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ticketflow.config import Config
from ticketflow.domain.errors import DependencyCycle
from ticketflow.domain.model import Attempt, Intent, Node, NodeState
from ticketflow.domain.parser import parse_body
from ticketflow.graph.ready import (
    blocked_on_escalated,
    detect_cycles,
    newly_ready,
    stagger_order,
)
from ticketflow.orchestrator.prompts import build_feedback, build_prompt
from ticketflow.ports.codehost import CodeHostPort, PrStatus, ReviewDecision
from ticketflow.ports.runner import (
    AttemptStatus,
    FailureClass,
    NodeDispatch,
    RunnerHandle,
    RunnerPort,
    ToolPolicy,
)
from ticketflow.ports.tracker import TrackerItem, TrackerPort
from ticketflow.store.store import Store

_K_SYNC_CURSOR = "cursor:tracker_sync"
_K_INTENT_CURSOR = "cursor:tracker_intents"
_K_BOARD_CURSOR = "cursor:board_projection"
_K_PAUSED = "dispatch_paused"
_K_IDLE_TICKS = "idle_ticks"


def _k_feedback(node_id: str) -> str:
    return f"feedback:{node_id}"


def _k_bootstrap(node_id: str) -> str:
    return f"bootstrap:{node_id}"


def _k_pr(node_id: str) -> str:
    return f"pr:{node_id}"


def _k_rerun(node_id: str, cycle: int) -> str:
    return f"rerun:{node_id}:{cycle}"


def _k_unresolved(node_id: str) -> str:
    return f"unresolved:{node_id}"


def _k_lease_expiries(node_id: str) -> str:
    return f"lease_expiries:{node_id}"


def _k_conflict(node_id: str) -> str:
    return f"conflict_attempted:{node_id}"


def derive_node_id(provider: str, external_key: str) -> str:
    """Deterministic node identity from the originating tracker item."""
    digest = hashlib.sha256(f"{provider}:{external_key}".encode()).hexdigest()
    return digest[:12]


def branch_for(node_id: str) -> str:
    return f"tf/{node_id}"


class WorkspaceProvider(Protocol):
    """Prepares the per-attempt workspace (ADR-0010). Idempotent."""

    def prepare(self, node_id: str, attempt: int, *, bootstrap: bool) -> Path: ...

    def diff_stat(self, node_id: str, attempt: int, base_branch: str) -> str:
        """git diff --stat of the attempt's work against the base branch.

        Empty output means an empty diff (ADR-0010: success is never stdout;
        the last judgement is a non-empty diff)."""
        ...


@dataclass
class TickReport:
    synced: int = 0
    intents_processed: int = 0
    dispatched: int = 0
    settled: int = 0
    merged: int = 0
    escalated: int = 0
    halted: bool = False
    graph_ok: bool = True
    notes: list[str] = field(default_factory=list)


class Orchestrator:
    """Deterministic scheduler + reconciler. The only writer of the store."""

    def __init__(
        self,
        *,
        store: Store,
        tracker: TrackerPort,
        runner: RunnerPort,
        codehost: CodeHostPort,
        workspaces: WorkspaceProvider,
        config: Config,
        clock: Callable[[], datetime],
        worker_id: str = "ticketflow",
        yolo: bool = False,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._runner = runner
        self._codehost = codehost
        self._workspaces = workspaces
        self._config = config
        self._clock = clock
        self._worker_id = worker_id
        self._yolo = yolo

    # -- public entry points ----------------------------------------------

    def tick(self) -> TickReport:
        report = TickReport()
        self._ingest_tracker_intents()
        self._consume_intents(report)
        self._sync_tracker(report)
        self._reconcile_attempts(report)
        self._settle_prs(report)
        self._compute_ready(report)
        self._dispatch(report)
        self._project_board()
        self._halt_heuristic(report)
        return report

    def adopt(self) -> None:
        """Re-attach in-flight work after a restart (ADR-0010).

        Adopts, never cleans up: live attempts keep running (their next poll
        harvests or extends), finished ones are harvested by the normal
        reconcile, and stale leases expire back to Ready.
        """
        now = self._clock()
        for attempt in self._store.running_attempts():
            self._store.append_event(
                "attempt_adopted",
                now=now,
                node_id=attempt.node_id,
                attempt=attempt.attempt,
                payload={"pid": attempt.pid},
            )

    # -- step 1: intents ---------------------------------------------------

    def _ingest_tracker_intents(self) -> None:
        now = self._clock()
        cursor = self._store.kv_get(_K_INTENT_CURSOR)
        tracker_intents, next_cursor = self._tracker.fetch_intents(cursor)
        for signal in tracker_intents:
            node_id = None
            payload = dict(signal.payload)
            if signal.external_key:
                payload["external_key"] = signal.external_key
                node_id = self._store.resolve_external(self._provider_name(), signal.external_key)
            self._store.add_intent(
                intent_type=signal.intent_type,
                source=self._provider_name(),
                node_id=node_id,
                payload=payload,
                external_id=signal.external_id,
                now=now,
            )
        if next_cursor:
            self._store.kv_set(_K_INTENT_CURSOR, next_cursor)

    def _consume_intents(self, report: TickReport) -> None:
        for intent in self._store.unprocessed_intents():
            now = self._clock()
            if intent.node_id is None and "external_key" in intent.payload:
                # The tracker signal arrived before its item was synced (sync
                # runs after intents in the tick). Resolve now; if the item is
                # still unknown, leave the intent pending for the next tick.
                resolved = self._store.resolve_external(
                    self._provider_name(), str(intent.payload["external_key"])
                )
                if resolved is None:
                    continue
                intent = Intent(
                    intent_id=intent.intent_id,
                    intent_type=intent.intent_type,
                    source=intent.source,
                    node_id=resolved,
                    payload=intent.payload,
                    created_at=intent.created_at,
                )
            self._apply_intent(intent, now)
            self._store.mark_intent_processed(intent.intent_id, now=now)
            report.intents_processed += 1

    def _apply_intent(self, intent: Intent, now: datetime) -> None:
        node = self._store.get_node(intent.node_id) if intent.node_id else None
        kind = intent.intent_type

        if kind == "resume" and node is None:
            # Global resume: clears a quota pause (ADR-0011).
            self._store.kv_delete(_K_PAUSED)
            self._store.append_event("dispatch_resumed", now=now, payload={"source": intent.source})
            return

        if kind in ("retry", "resume", "unblock") and node is not None:
            if node.state is NodeState.ESCALATED:
                feedback = intent.payload.get("feedback")
                if isinstance(feedback, str) and feedback:
                    self._store.kv_set(_k_feedback(node.node_id), feedback)
                self._store.reset_counters(node.node_id, now=now)
                self._store.kv_delete(_k_lease_expiries(node.node_id))
                self._store.set_state(node.node_id, NodeState.READY, now=now)
                return
            if kind == "unblock" and node.state is NodeState.BLOCKED:
                # Blocked -> Ready keeps its ADR-0006 guard even under an
                # operator override: every stored upstream edge must be
                # resolved, and no ancestor may be Escalated. What unblock
                # overrides is only the unresolved-key hold (ADR-0007).
                upstream_states = [
                    upstream_node.state
                    for upstream in self._store.upstreams_of(node.node_id)
                    if (upstream_node := self._store.get_node(upstream)) is not None
                ]
                blocked_by = None
                if any(state is not NodeState.MERGED for state in upstream_states):
                    blocked_by = "upstream edges unresolved"
                elif self._has_escalated_ancestor(node.node_id):
                    blocked_by = "escalated ancestor"
                if blocked_by:
                    self._store.append_event(
                        "intent_unhandled",
                        now=now,
                        node_id=node.node_id,
                        payload={
                            "type": kind,
                            "source": intent.source,
                            "reason": blocked_by,
                        },
                    )
                    return
                self._store.kv_delete(_k_unresolved(node.node_id))
                self._store.set_state(node.node_id, NodeState.READY, now=now)
                return

        elif kind in ("cancel", "reject") and node is not None:
            reason = f"{kind} by {intent.source}"
            if node.state is NodeState.IN_PROGRESS:
                self._cancel_running_attempt(node)
                self._store.release_lease(node.node_id)
                self._store.set_state(node.node_id, NodeState.ESCALATED, reason=reason, now=now)
                return
            if node.state in (NodeState.BLOCKED, NodeState.READY):
                self._store.set_state(node.node_id, NodeState.ESCALATED, reason=reason, now=now)
                return

        self._store.append_event(
            "intent_unhandled",
            now=now,
            node_id=intent.node_id,
            payload={"type": kind, "source": intent.source},
        )

    def _cancel_running_attempt(self, node: Node) -> None:
        attempt = self._store.get_attempt(node.node_id, node.attempt_count)
        if attempt and attempt.status == "running":
            self._runner.cancel(self._handle_from_attempt(attempt))
            self._store.update_attempt(
                node.node_id, attempt.attempt, status="cancelled", finished_at=self._clock()
            )

    # -- step 2: tracker sync ----------------------------------------------

    def _sync_tracker(self, report: TickReport) -> None:
        now = self._clock()
        cursor = self._store.kv_get(_K_SYNC_CURSOR)
        items, next_cursor = self._tracker.fetch_nodes(cursor)

        parsed_items: list[tuple[str, TrackerItem, tuple[str, ...]]] = []
        for item in items:
            node_id, changed = self._upsert_item(item, now)
            parsed = parse_body(item.body)
            if changed and parsed.issues:
                # Report malformed blocks: an event AND a comment on the
                # issue — the teaching mechanism of ADR-0007. Only on
                # content change, so syncs do not repeat the comment.
                for issue in parsed.issues:
                    self._store.append_event(
                        "body_parse_issue", now=now, node_id=node_id, payload={"issue": issue}
                    )
                listed = "\n".join(f"- {issue}" for issue in parsed.issues)
                self._tracker.push_comment(
                    item.external_key,
                    "ticketflow could not fully parse this issue body:\n"
                    f"{listed}\n\nMalformed entries are ignored, never guessed at.",
                )
            parsed_items.append((node_id, item, parsed.depends_on))
            report.synced += 1

        for node_id, item, depends_on in parsed_items:
            upstream_ids = []
            unresolved: list[str] = []
            for key in depends_on:
                upstream = self._store.resolve_external(item.provider, key)
                if upstream is None:
                    unresolved.append(key)
                    self._store.append_event(
                        "dependency_unresolved",
                        now=now,
                        node_id=node_id,
                        payload={"key": key},
                    )
                else:
                    upstream_ids.append(upstream)
            self._store.replace_upstreams(node_id, upstream_ids)
            if unresolved:
                # An unresolved dependency HOLDS the node (reported, never
                # guessed at, ADR-0007); a human unblock intent overrides.
                self._store.kv_set(_k_unresolved(node_id), json.dumps(unresolved))
                node = self._store.get_node(node_id)
                if node and node.state is NodeState.BLOCKED:
                    self._store.set_blocked_reason(
                        node_id, f"unresolved dependencies: {', '.join(unresolved)}", now=now
                    )
            else:
                self._store.kv_delete(_k_unresolved(node_id))

        try:
            detect_cycles(self._store.all_edges())
        except DependencyCycle as exc:
            report.graph_ok = False
            report.notes.append(str(exc))
            self._store.append_event("graph_cycle", now=now, payload={"error": str(exc)})

        if next_cursor:
            self._store.kv_set(_K_SYNC_CURSOR, next_cursor)

    def _upsert_item(self, item: TrackerItem, now: datetime) -> tuple[str, bool]:
        """Insert or refresh one tracker item; returns (node_id, changed)."""
        node_id = self._store.resolve_external(item.provider, item.external_key)
        parsed = parse_body(item.body)
        if node_id is None:
            node_id = derive_node_id(item.provider, item.external_key)
            state = NodeState.MERGED if item.closed else NodeState.BLOCKED
            self._store.insert_node(
                node_id=node_id,
                title=item.title,
                body=item.body,
                state=state,
                scope_hints=parsed.scope,
                now=now,
            )
            self._store.link_external(
                node_id, provider=item.provider, external_key=item.external_key, etag=item.etag
            )
            self._store.append_event(
                "node_synced",
                now=now,
                node_id=node_id,
                payload={"external_key": item.external_key, "state": state.value},
            )
            return node_id, True
        existing = self._store.get_node(node_id)
        changed = bool(existing and (existing.title != item.title or existing.body != item.body))
        if changed:
            self._store.update_node_content(
                node_id,
                title=item.title,
                body=item.body,
                scope_hints=parsed.scope,
                now=now,
            )
        self._store.link_external(
            node_id, provider=item.provider, external_key=item.external_key, etag=item.etag
        )
        return node_id, changed

    # -- step 3: reconcile running attempts --------------------------------

    def _reconcile_attempts(self, report: TickReport) -> None:
        now = self._clock()
        for node_id in self._store.expire_stale_leases(now=now):
            node = self._store.get_node(node_id)
            if node and node.state is NodeState.IN_PROGRESS:
                # The process is presumed dead (a live one gets its lease
                # extended every poll): retire its attempt so it stops
                # holding dispatch capacity.
                stale = self._store.get_attempt(node_id, node.attempt_count)
                if stale and stale.status == "running":
                    self._store.update_attempt(
                        node_id, stale.attempt, status="aborted", finished_at=now
                    )
                expiries = int(self._store.kv_get(_k_lease_expiries(node_id)) or "0") + 1
                self._store.kv_set(_k_lease_expiries(node_id), str(expiries))
                self._store.append_event(
                    "lease_expired",
                    now=now,
                    node_id=node_id,
                    attempt=node.attempt_count or None,
                )
                if expiries >= self._config.limits.max_attempts:
                    # ADR-0006 trigger: repeated lease expiry — the process
                    # keeps dying without a heartbeat.
                    self._escalate(node, f"repeated lease expiry ({expiries})", report)
                else:
                    self._store.set_state(
                        node_id, NodeState.READY, now=now, attempt=node.attempt_count or None
                    )

        for attempt in self._store.running_attempts():
            now = self._clock()
            node = self._store.get_node(attempt.node_id)
            if node is None:
                continue
            result = self._runner.poll(self._handle_from_attempt(attempt))

            if result.status is AttemptStatus.RUNNING:
                self._store.extend_lease(
                    attempt.node_id,
                    ttl_seconds=self._config.limits.lease_ttl_seconds,
                    now=now,
                )
                continue

            if result.status is AttemptStatus.TIMED_OUT:
                self._runner.cancel(self._handle_from_attempt(attempt))
                self._store.update_attempt(
                    attempt.node_id, attempt.attempt, status="timed_out", finished_at=now
                )
                self._store.release_lease(attempt.node_id)
                self._escalate(node, "wall-clock timeout", report)
                continue

            # Exited.
            self._store.update_attempt(
                attempt.node_id,
                attempt.attempt,
                status="exited",
                exit_code=result.exit_code,
                session_id=result.session_id,
                finished_at=now,
            )
            self._store.release_lease(attempt.node_id)
            if result.cost is not None:
                self._store.append_event(
                    "attempt_cost",
                    now=now,
                    node_id=attempt.node_id,
                    attempt=attempt.attempt,
                    payload={"cost": result.cost},
                )

            if result.exit_code == 0:
                self._harvest_success(node, attempt.attempt, report)
            else:
                self._handle_failure(node, result.failure_class, report)

    def _harvest_success(self, node: Node, attempt: int, report: TickReport) -> None:
        now = self._clock()
        node_id = node.node_id

        if self._store.kv_get(_k_bootstrap(node_id)):
            self._store.kv_delete(_k_bootstrap(node_id))
            if self._codehost.repo_exists():
                self._store.append_event(
                    "merged",
                    now=now,
                    node_id=node_id,
                    attempt=attempt,
                    payload={"how": "bootstrap-push", "checks": []},
                )
                self._store.set_state(node_id, NodeState.MERGED, now=now, attempt=attempt)
                report.merged += 1
            else:
                self._escalate(node, "bootstrap exit clean but repo still missing", report)
            return

        branch = branch_for(node_id)
        if not self._codehost.branch_exists(branch):
            # Success is never stdout: a clean exit with nothing pushed is the
            # empty-diff escalation (ADR-0010).
            self._escalate(node, "clean exit, empty diff (no branch pushed)", report)
            return
        base = self._codehost.default_branch() or "main"
        if not self._workspaces.diff_stat(node_id, attempt, base).strip():
            # Exit code, then checks, then a NON-EMPTY diff: a pushed branch
            # identical to the base is still an empty diff (ADR-0010).
            self._escalate(node, f"clean exit, empty diff (branch identical to {base})", report)
            return

        self._store.kv_delete(_k_lease_expiries(node_id))
        pr_number = self._codehost.find_pr_for_branch(branch)
        if pr_number is None:
            pr_number = self._codehost.open_pr(
                branch,
                node.title,
                f"Automated change for node `{node_id}`.\n\n{node.body}".strip(),
            )
            self._store.append_event(
                "pr_opened",
                now=now,
                node_id=node_id,
                attempt=attempt,
                payload={"pr": pr_number},
            )
        self._store.kv_set(_k_pr(node_id), str(pr_number))

        self._collect_handoff(node_id, attempt, pr_number)

        if node.state in (NodeState.IN_PROGRESS, NodeState.ADDRESSING_FEEDBACK):
            self._store.set_state(node_id, NodeState.AWAITING_SIGNALS, now=now, attempt=attempt)

    def _collect_handoff(self, node_id: str, attempt: int, pr_number: int) -> None:
        workspace = self._workspaces.prepare(node_id, attempt, bootstrap=False)
        handoff_path = workspace / "handoff.md"
        if handoff_path.is_file():
            content = handoff_path.read_text(encoding="utf-8")
            self._store.set_handoff(node_id, content, now=self._clock())
            self._codehost.post_comment(pr_number, f"## Handoff\n\n{content}")

    def _handle_failure(self, node: Node, failure: FailureClass, report: TickReport) -> None:
        now = self._clock()
        if failure is FailureClass.QUOTA:
            self._store.kv_set(_K_PAUSED, "provider quota exhausted")
            self._store.append_event("dispatch_paused", now=now, payload={"reason": "quota"})
            self._escalate(node, "provider quota exhausted", report)
            return

        if node.state is NodeState.ADDRESSING_FEEDBACK:
            # Let the settle loop re-detect the feedback and try again; the
            # cycle cap bounds this.
            self._store.set_state(node.node_id, NodeState.AWAITING_SIGNALS, now=now)
            return

        if node.attempt_count >= self._config.limits.max_attempts:
            self._escalate(node, "runner crashed repeatedly", report)
        else:
            self._store.append_event(
                "attempt_failed", now=now, node_id=node.node_id, attempt=node.attempt_count
            )
            self._store.set_state(node.node_id, NodeState.READY, now=now)

    # -- step 4: settle PRs -------------------------------------------------

    def _settle_prs(self, report: TickReport) -> None:
        for node in self._store.list_nodes(state=NodeState.AWAITING_SIGNALS):
            self._settle_node(node, report)

    def _settle_node(self, node: Node, report: TickReport) -> None:
        now = self._clock()
        node_id = node.node_id
        raw_pr = self._store.kv_get(_k_pr(node_id))
        if raw_pr is None:
            return
        pr_number = int(raw_pr)
        status = self._codehost.get_pr_status(pr_number)
        report.settled += 1

        if status.state == "merged":
            self._record_merge(node, pr_number, status, how="host")
            self._store.set_state(
                node_id, NodeState.MERGED, now=now, attempt=node.attempt_count or None
            )
            report.merged += 1
            return
        if status.checks_pending:
            return  # settle window: wait until every check has reported

        failed = status.checks_failed
        rerun_key = _k_rerun(node_id, node.cycle_count)
        if failed and self._store.kv_get(rerun_key) is None:
            # Flaky handling (ADR-0009): one re-run before it becomes the
            # agent's problem.
            self._store.kv_set(rerun_key, json.dumps(sorted(failed)))
            self._codehost.rerun_failed_checks(pr_number)
            self._store.append_event(
                "checks_rerun",
                now=now,
                node_id=node_id,
                attempt=node.attempt_count or None,
                payload={"checks": sorted(failed)},
            )
            return
        if (raw := self._store.kv_get(rerun_key)) is not None:
            previously_failed: list[str] = json.loads(raw)
            for name in previously_failed:
                self._store.record_check_outcome(
                    name,
                    flaked=name not in failed,
                    now=now,
                    node_id=node_id,
                    attempt=node.attempt_count or None,
                )
            self._store.kv_delete(rerun_key)

        changes_requested = status.review_decision is ReviewDecision.CHANGES_REQUESTED
        needs_work = bool(failed) or status.unresolved_threads > 0 or changes_requested
        if needs_work:
            self._enter_feedback_cycle(node, pr_number, failed, changes_requested, report)
            return

        if status.mergeable is False:
            # Conflict resolution gets a tighter leash than normal feedback:
            # one narrow attempt, then escalate (ADR-0008, spec §12.1) — this
            # is where agents silently discard other people's work.
            if self._store.kv_get(_k_conflict(node_id)) is not None:
                self._escalate(
                    node,
                    "merge conflict unresolved after one resolution attempt",
                    report,
                    attempt=node.attempt_count or None,
                )
                return
            self._store.kv_set(_k_conflict(node_id), "1")
            base = self._codehost.default_branch() or "main"
            feedback = (
                "The pull request cannot merge because of a rebase conflict with "
                f"`{base}`. Rebase your branch onto `origin/{base}`, resolve the "
                "conflicts — preserving BOTH your changes and the other work; do "
                "not discard anyone's changes — then force-push the branch. Do "
                "nothing else."
            )
            self._dispatch_resume(node, feedback, "conflict_redispatch", {"pr": pr_number})
            return

        # Green, threads resolved. Walk the rest of the ladder.
        if status.review_decision in (
            ReviewDecision.APPROVED,
            ReviewDecision.NONE,
        ) and self._codehost.merge(pr_number):
            self._record_merge(node, pr_number, status, how="ticketflow")
            self._store.set_state(
                node_id, NodeState.MERGED, now=now, attempt=node.attempt_count or None
            )
            report.merged += 1
            return
        if self._codehost.enable_auto_merge(pr_number):
            self._store.append_event(
                "auto_merge_enabled",
                now=now,
                node_id=node_id,
                attempt=node.attempt_count or None,
                payload={"pr": pr_number},
            )
        # Otherwise: waiting on approvals; ask again next settle.

    def _enter_feedback_cycle(
        self,
        node: Node,
        pr_number: int,
        failed: tuple[str, ...],
        changes_requested: bool,
        report: TickReport,
    ) -> None:
        now = self._clock()
        node_id = node.node_id
        cycle = self._store.bump_cycle_count(node_id, now=now)
        if cycle > self._config.limits.cycle_cap:
            self._escalate(node, f"cycle cap exceeded ({cycle})", report)
            return

        comments = self._codehost.get_feedback(pr_number, None)
        feedback = build_feedback(
            failed_checks=failed,
            comments=[
                (
                    c.author,
                    f"{c.path}:{c.line}" if c.path and c.line else c.path,
                    c.body,
                )
                for c in comments
            ],
            changes_requested=changes_requested,
        )
        self._dispatch_resume(
            node, feedback, "feedback_dispatched", {"cycle": cycle, "failed_checks": list(failed)}
        )

    def _dispatch_resume(
        self, node: Node, feedback: str, event_kind: str, payload: dict[str, Any]
    ) -> None:
        """Resume the node's session with feedback as a new leased attempt."""
        now = self._clock()
        node_id = node.node_id
        last_attempt = self._store.get_attempt(node_id, node.attempt_count)
        next_attempt = self._store.bump_attempt_count(node_id, now=now)
        if not self._store.claim_lease(
            node_id,
            worker_id=self._worker_id,
            attempt=next_attempt,
            ttl_seconds=self._config.limits.lease_ttl_seconds,
            now=now,
        ):
            return
        run_dir = self._config.runs_dir / node_id / str(next_attempt)
        workspace = self._workspaces.prepare(node_id, next_attempt, bootstrap=False)
        template = RunnerHandle(
            node_id=node_id,
            attempt=next_attempt,
            pid=0,
            create_time=0.0,
            run_dir=run_dir,
            session_id=last_attempt.session_id if last_attempt else None,
            workspace=workspace,
        )
        self._store.create_attempt(
            node_id,
            attempt=next_attempt,
            runner=self._config.runner.name,
            run_dir=str(run_dir),
            model=self._config.runner.model,
            now=now,
        )
        handle = self._runner.resume(template, feedback)
        self._store.update_attempt(
            node_id,
            next_attempt,
            pid=handle.pid,
            create_time=handle.create_time,
            session_id=handle.session_id,
        )
        self._store.set_state(node_id, NodeState.ADDRESSING_FEEDBACK, now=now, attempt=next_attempt)
        self._store.append_event(
            event_kind,
            now=now,
            node_id=node_id,
            attempt=next_attempt,
            payload=payload,
        )

    # -- step 5: ready-set --------------------------------------------------

    def _compute_ready(self, report: TickReport) -> None:
        if not report.graph_ok:
            return
        now = self._clock()
        states = {n.node_id: n.state for n in self._store.list_nodes()}
        edges = self._store.all_edges()
        for node_id in newly_ready(states, edges):
            if self._store.kv_get(_k_unresolved(node_id)) is not None:
                continue
            self._store.set_state(node_id, NodeState.READY, now=now)
            states[node_id] = NodeState.READY
        for node_id, ancestor in blocked_on_escalated(states, edges).items():
            reason = f"blocked by escalated {ancestor}"
            node = self._store.get_node(node_id)
            if node and node.blocked_reason != reason:
                self._store.set_blocked_reason(node_id, reason, now=now)
                self._store.append_event(
                    "blocked_on_escalated",
                    now=now,
                    node_id=node_id,
                    payload={"ancestor": ancestor},
                )

    # -- step 6: dispatch ---------------------------------------------------

    def _dispatch(self, report: TickReport) -> None:
        if (pause := self._store.kv_get(_K_PAUSED)) is not None:
            report.notes.append(f"dispatch paused: {pause}")
            return

        running = self._store.running_attempts()
        capacity = self._config.limits.max_parallel - len(running)
        if capacity <= 0:
            return

        in_flight_scopes = []
        for attempt in running:
            node = self._store.get_node(attempt.node_id)
            if node:
                in_flight_scopes.append(node.scope_hints)

        ready = [(n.node_id, n.scope_hints) for n in self._store.list_nodes(state=NodeState.READY)]
        ordered = stagger_order(ready, in_flight_scopes=in_flight_scopes)

        for node_id, _scopes in ordered[:capacity]:
            self._dispatch_node(node_id, report)

    def _dispatch_node(self, node_id: str, report: TickReport) -> None:
        now = self._clock()
        node = self._store.get_node(node_id)
        if node is None:
            return
        next_attempt = self._store.bump_attempt_count(node_id, now=now)
        if not self._store.claim_lease(
            node_id,
            worker_id=self._worker_id,
            attempt=next_attempt,
            ttl_seconds=self._config.limits.lease_ttl_seconds,
            now=now,
        ):
            return

        bootstrap = not self._codehost.repo_exists()
        workspace = self._workspaces.prepare(node_id, next_attempt, bootstrap=bootstrap)
        run_dir = self._config.runs_dir / node_id / str(next_attempt)

        upstream_handoffs = {}
        for upstream in self._store.upstreams_of(node_id):
            handoff = self._store.get_handoff(upstream)
            if handoff:
                upstream_handoffs[upstream] = handoff

        feedback = self._store.kv_get(_k_feedback(node_id))
        if feedback is not None:
            self._store.kv_delete(_k_feedback(node_id))

        prompt = build_prompt(
            title=node.title,
            body=node.body,
            repo=self._config.codehost.repo,
            branch=branch_for(node_id),
            default_branch=self._codehost.default_branch() or "main",
            bootstrap=bootstrap,
            scope_hints=node.scope_hints,
            upstream_handoffs=upstream_handoffs,
            feedback=feedback,
        )
        policy = ToolPolicy(
            allowed_tools=self._config.runner.allowed_tools,
            disallowed_tools=self._config.runner.disallowed_tools,
            yolo=self._yolo,
        )
        dispatch = NodeDispatch(
            node_id=node_id,
            attempt=next_attempt,
            prompt=prompt,
            run_dir=run_dir,
            model=self._config.runner.model,
        )
        self._store.create_attempt(
            node_id,
            attempt=next_attempt,
            runner=self._config.runner.name,
            run_dir=str(run_dir),
            model=self._config.runner.model,
            now=now,
        )
        if bootstrap:
            self._store.kv_set(_k_bootstrap(node_id), "1")

        handle = self._runner.start(dispatch, workspace, policy)
        self._store.update_attempt(
            node_id,
            next_attempt,
            pid=handle.pid,
            create_time=handle.create_time,
            session_id=handle.session_id,
        )
        self._store.set_state(node_id, NodeState.IN_PROGRESS, now=now, attempt=next_attempt)
        self._store.append_event(
            "dispatched",
            now=now,
            node_id=node_id,
            attempt=next_attempt,
            payload={"bootstrap": bootstrap, "yolo": self._yolo},
        )
        report.dispatched += 1

    # -- step 7: board projection (reads the event log, ADR-0005) -----------

    def _project_board(self) -> None:
        cursor = int(self._store.kv_get(_K_BOARD_CURSOR) or "0")
        last = cursor
        for event in self._store.events_after(cursor):
            last = event.event_id
            if event.kind != "state_changed" or event.node_id is None:
                continue
            state = NodeState(event.payload["to"])
            for ref in self._store.refs_for(event.node_id):
                if ref.provider == self._provider_name():
                    self._tracker.push_state(ref.external_key, state)
                    if state is NodeState.ESCALATED and event.payload.get("reason"):
                        self._tracker.push_comment(
                            ref.external_key,
                            f"Escalated: {event.payload['reason']}. "
                            "A human needs to look at this node.",
                        )
        if last != cursor:
            self._store.kv_set(_K_BOARD_CURSOR, str(last))

    # -- halt heuristic (ADR-0006) ------------------------------------------

    def _halt_heuristic(self, report: TickReport) -> None:
        escalated = self._store.list_nodes(state=NodeState.ESCALATED)
        active = self._store.running_attempts()
        idle = report.dispatched == 0 and not active
        if not (escalated and idle):
            self._store.kv_set(_K_IDLE_TICKS, "0")
            return
        ticks = int(self._store.kv_get(_K_IDLE_TICKS) or "0") + 1
        self._store.kv_set(_K_IDLE_TICKS, str(ticks))
        if ticks >= self._config.limits.halt_ticks:
            report.halted = True
            self._store.append_event(
                "halted",
                now=self._clock(),
                payload={"idle_ticks": ticks, "escalated": len(escalated)},
            )

    # -- helpers -------------------------------------------------------------

    def _escalate(
        self, node: Node, reason: str, report: TickReport, *, attempt: int | None = None
    ) -> None:
        self._store.set_state(
            node.node_id,
            NodeState.ESCALATED,
            reason=reason,
            now=self._clock(),
            attempt=attempt if attempt is not None else node.attempt_count or None,
        )
        report.escalated += 1

    def _record_merge(self, node: Node, pr_number: int, status: PrStatus, *, how: str) -> None:
        """Record how the merge happened and which checks reported (ADR-0009).

        Observation, not validation: this is what makes "we merged 40 PRs
        with no gates" visible after the fact.
        """
        self._store.append_event(
            "merged",
            now=self._clock(),
            node_id=node.node_id,
            attempt=node.attempt_count or None,
            payload={
                "pr": pr_number,
                "how": how,
                "checks": [{"name": c.name, "state": c.state.value} for c in status.checks],
                "review_decision": status.review_decision.value,
            },
        )

    def _has_escalated_ancestor(self, node_id: str) -> bool:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for upstream in self._store.upstreams_of(stack.pop()):
                if upstream in seen:
                    continue
                seen.add(upstream)
                upstream_node = self._store.get_node(upstream)
                if upstream_node and upstream_node.state is NodeState.ESCALATED:
                    return True
                stack.append(upstream)
        return False

    def _provider_name(self) -> str:
        return self._config.tracker.provider

    @staticmethod
    def _handle_from_attempt(attempt: Attempt) -> RunnerHandle:
        return RunnerHandle(
            node_id=attempt.node_id,
            attempt=attempt.attempt,
            pid=attempt.pid or 0,
            create_time=attempt.create_time or 0.0,
            run_dir=Path(attempt.run_dir),
            session_id=attempt.session_id,
        )

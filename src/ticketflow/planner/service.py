"""The planner service (ADR-0014): one resumable turn per public method.

No process stays resident across the review — every method loads state from
the store, does one turn, and returns (spec §13.5). Validation runs on every
revision, whatever its source: synthesis output, an agent revision turn, or
a human ``$EDITOR`` edit; a failing revision rejects the turn, never the
plan. The YAML working copy is rewritten on every accepted turn,
unconditionally — never gated on yolo (ADR-0013).

The planner CLI is the second scoped store writer of ADR-0003's revision:
plan* tables, ``processed_at`` on ``plan_*`` intents, and events. It never
writes nodes, edges, leases, attempts or kv.
"""

import time
from collections.abc import Callable
from datetime import datetime

from ticketflow.config import Config
from ticketflow.domain.errors import (
    PlanEmitFailed,
    PlanTurnRefused,
    PlanValidationError,
    UnknownEpic,
)
from ticketflow.domain.plan import PlanRecord, PlanStatus
from ticketflow.planner.approval import consume_plan_intents, yaml_sha256
from ticketflow.planner.emit import EmitReport, run_emit
from ticketflow.planner.grounding import WorkspaceProvider, run_grounding
from ticketflow.planner.prompts import build_grounding_prompt
from ticketflow.planner.schema import Plan, derive_plan_id
from ticketflow.planner.synthesis import PlanSynthesizer, RevisionRequest, SynthesisRequest
from ticketflow.planner.validate import semantic_errors
from ticketflow.planner.yaml_io import dump_plan, load_plan, plan_filename, write_plan_file
from ticketflow.ports.runner import RunnerPort
from ticketflow.ports.tracker import TrackerItem, TrackerPort
from ticketflow.store.store import Store


class Planner:
    """Offline planning turns in front of the deterministic scheduler."""

    def __init__(
        self,
        *,
        store: Store,
        tracker: TrackerPort,
        runner: RunnerPort,
        synthesizer: PlanSynthesizer,
        workspaces: WorkspaceProvider,
        config: Config,
        repo_exists: Callable[[], bool],
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
        yolo: bool = False,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._runner = runner
        self._synthesizer = synthesizer
        self._workspaces = workspaces
        self._config = config
        self._repo_exists = repo_exists
        self._clock = clock
        self._sleep = sleep
        self._yolo = yolo

    # -- lifecycle turns ----------------------------------------------------

    def new(self, epic_key: str, *, replan: bool = False) -> PlanRecord:
        """Ingest → ground → synthesize, resuming from wherever a previous
        run stopped. Under ``--yolo`` the approval intent is written and
        emission chained in the same invocation (ADR-0013)."""
        plan = self.ingest(epic_key, replan=replan)
        if plan.status in (PlanStatus.INGESTED, PlanStatus.GROUNDING):
            self.ground(epic_key)
            plan = self._require_plan(epic_key)
        if plan.status is PlanStatus.SYNTHESIS:
            self.synthesize(epic_key)
            plan = self._require_plan(epic_key)
        if self._yolo and plan.status is PlanStatus.IN_REVIEW:
            self.request_approval(epic_key, source="yolo")
            report = self.emit(epic_key)
            if report.failure:
                # Unattended runs must not swallow a partial emission.
                raise PlanEmitFailed(report.failure)
            refreshed = self._store.get_plan(plan.plan_id)
            assert refreshed is not None
            plan = refreshed
        return plan

    def ingest(self, epic_key: str, *, replan: bool = False) -> PlanRecord:
        provider = self._provider()
        existing = self._store.plan_for_epic(provider, epic_key)
        if existing is not None:
            # Apply anything pending first: a rejection recorded since the
            # last turn discards the old plan here, which is what lets
            # `plan new` re-plan a rejected epic (ADR-0004).
            existing = consume_plan_intents(self._store, existing, clock=self._clock)
            if existing.status is not PlanStatus.DISCARDED:
                return existing
        latest = self._store.latest_plan_for_epic(provider, epic_key)
        if latest is not None and latest.status is PlanStatus.EMITTED and not replan:
            # Re-planning an emitted epic emits a SECOND set of children —
            # a human decision (spec §13.6), never a default.
            raise PlanTurnRefused(
                f"{epic_key} was already planned and emitted as {latest.plan_id}; "
                "re-run with --re-plan to deliberately plan a second wave"
            )
        self._fetch_epic(epic_key)  # fail before creating anything
        now = self._clock()
        plan_id = derive_plan_id(provider, epic_key, now)
        self._store.create_plan(plan_id=plan_id, provider=provider, epic_key=epic_key, now=now)
        plan = self._store.get_plan(plan_id)
        assert plan is not None
        return plan

    def ground(self, epic_key: str) -> str:
        plan = self._require_plan(epic_key)
        if plan.status not in (PlanStatus.INGESTED, PlanStatus.GROUNDING):
            raise PlanTurnRefused(f"plan is {plan.status.value}; grounding is done")
        epic = self._fetch_epic(epic_key)
        greenfield = not self._repo_exists()
        prompt = build_grounding_prompt(
            epic_title=epic.title,
            epic_body=epic.body,
            repo=self._config.codehost.repo,
            greenfield=greenfield,
            upstream_handoffs=self._upstream_handoffs(epic_key),
            today=self._clock().date().isoformat(),
        )
        return run_grounding(
            store=self._store,
            runner=self._runner,
            workspaces=self._workspaces,
            config=self._config,
            plan=plan,
            prompt=prompt,
            greenfield=greenfield,
            yolo=self._yolo,
            clock=self._clock,
            sleep=self._sleep,
        )

    def synthesize(self, epic_key: str) -> int:
        """First proposal: revision 1, status ``in_review``."""
        plan = self._require_plan(epic_key)
        if plan.status is not PlanStatus.SYNTHESIS:
            raise PlanTurnRefused(f"plan is {plan.status.value}; expected synthesis")
        assert plan.brief is not None  # the grounding turn stored it
        epic = self._fetch_epic(epic_key)
        proposed = self._synthesizer.synthesize(
            SynthesisRequest(
                plan_id=plan.plan_id,
                epic_key=epic_key,
                epic_title=epic.title,
                epic_body=epic.body,
                brief=plan.brief,
                greenfield=not self._repo_exists(),
            )
        )
        self._check(proposed, plan)
        revision = self._record_revision(plan, proposed, source="synthesis")
        self._store.set_plan_status(plan.plan_id, PlanStatus.IN_REVIEW, now=self._clock())
        self._store.append_event(
            "plan_proposed",
            now=self._clock(),
            payload={
                "plan_id": plan.plan_id,
                "revision": revision,
                "items": len(proposed.items),
                "edges": len(proposed.edges),
                "unevidenced_edges": len(proposed.unevidenced_edges),
            },
        )
        return revision

    def revise(self, epic_key: str, feedback: str) -> int:
        """One conversational revision turn: stateless synthesis over
        (current YAML, feedback, brief). A failing revision rejects the
        turn — the previous revision stands (spec §13.5 rule 1)."""
        plan = self._require_plan(epic_key)
        if plan.status is not PlanStatus.IN_REVIEW:
            raise PlanTurnRefused(f"plan is {plan.status.value}; expected in_review")
        current = self._store.get_plan_revision(plan.plan_id, plan.current_revision)
        assert current is not None
        try:
            revised = self._synthesizer.revise(
                RevisionRequest(
                    plan_id=plan.plan_id,
                    epic_key=epic_key,
                    current_plan_yaml=current.yaml,
                    feedback=feedback,
                    brief=plan.brief or "",
                )
            )
            self._check(revised, plan)
        except PlanValidationError as exc:
            self._store.append_event(
                "plan_validation_failed",
                now=self._clock(),
                payload={"plan_id": plan.plan_id, "turn": "revision", "error": str(exc)},
            )
            raise
        revision = self._record_revision(plan, revised, source="revision")
        self._store.append_event(
            "plan_revised",
            now=self._clock(),
            payload={"plan_id": plan.plan_id, "revision": revision, "source": "revision"},
        )
        return revision

    def validate_file(self, epic_key: str) -> int | None:
        """Ingest a hand-edited ``plans/<epic-key>.yaml`` as a revision.

        Returns the new revision number, or None when the file matches the
        current revision. Refused once the plan is approved — the graph
        never mutates after approval (ADR-0014)."""
        plan = self._require_plan(epic_key)
        if plan.status is not PlanStatus.IN_REVIEW:
            raise PlanTurnRefused(
                f"plan is {plan.status.value}; the file is only editable in review"
            )
        path = self._config.plans_dir / plan_filename(epic_key)
        if not path.is_file():
            raise PlanTurnRefused(f"no plan file at {path}")
        try:
            edited = load_plan(path.read_text(encoding="utf-8"))
            self._check(edited, plan)
        except PlanValidationError as exc:
            # Reject the turn, not the plan: record nothing, leave the file
            # on disk for fixing (spec §13.5 rule 1).
            self._store.append_event(
                "plan_validation_failed",
                now=self._clock(),
                payload={"plan_id": plan.plan_id, "turn": "human_edit", "error": str(exc)},
            )
            raise
        current = self._store.get_plan_revision(plan.plan_id, plan.current_revision)
        assert current is not None
        normalized = dump_plan(edited)
        if normalized == current.yaml:
            return None
        revision = self._store.add_plan_revision(
            plan.plan_id, yaml_text=normalized, source="human_edit", now=self._clock()
        )
        write_plan_file(self._config.plans_dir, epic_key, normalized)
        self._store.append_event(
            "plan_revised",
            now=self._clock(),
            payload={"plan_id": plan.plan_id, "revision": revision, "source": "human_edit"},
        )
        return revision

    # -- approval and emission ----------------------------------------------

    def request_approval(
        self, epic_key: str, revision: int | None = None, *, source: str = "cli"
    ) -> int | None:
        """Validate, then write the ``plan_approve`` intent (ADR-0004).

        Pending hand edits are ingested first, so approval always targets
        what is on disk. Returns None when this exact (plan, revision)
        approval was already recorded — double submission dedupes on
        ``external_id``."""
        plan = self._require_plan(epic_key)
        if plan.status is not PlanStatus.IN_REVIEW:
            raise PlanTurnRefused(f"plan is {plan.status.value}; expected in_review")
        working_copy = self._config.plans_dir / plan_filename(epic_key)
        if working_copy.is_file():
            self.validate_file(epic_key)
        else:
            # SQLite is the truth (ADR-0003): a deleted working copy is
            # regenerated from the current revision, not an error.
            current = self._store.get_plan_revision(plan.plan_id, plan.current_revision)
            assert current is not None
            write_plan_file(self._config.plans_dir, epic_key, current.yaml)
        plan = self._require_plan(epic_key)
        target = revision if revision is not None else plan.current_revision
        if target != plan.current_revision:
            # Approval is digest-pinned to current content; approving an
            # older revision could only ever be refused as stale at
            # consumption — refuse it here, where the operator can act.
            raise PlanTurnRefused(
                f"revision {target} is not the latest ({plan.current_revision}); "
                "only the latest revision can be approved"
            )
        blob = self._store.get_plan_revision(plan.plan_id, target)
        if blob is None:
            raise PlanTurnRefused(f"no revision {target} for plan {plan.plan_id}")
        return self._store.add_intent(
            intent_type="plan_approve",
            source=source,
            node_id=None,
            payload={
                "plan_id": plan.plan_id,
                "revision": target,
                "yaml_sha256": yaml_sha256(blob.yaml),
            },
            external_id=f"plan_approve:{plan.plan_id}:{target}",
            now=self._clock(),
        )

    def request_rejection(self, epic_key: str, reason: str, *, source: str = "cli") -> int | None:
        """Write the ``plan_reject`` intent, then consume it in the same turn.

        Rejection has no later natural turn the way approval has ``emit``,
        so this turn applies it immediately — the signal still enters
        through the intents table (ADR-0004), exactly like yolo's
        auto-approval does. A pending, never-consumed approval is superseded:
        the reject is the human's last word, and while the emission ledger is
        empty retracting the approval loses nothing. Once anything is
        emitted there is no rollback."""
        plan = self._require_plan(epic_key)
        plan = consume_plan_intents(self._store, plan, clock=self._clock)
        if plan.status is PlanStatus.EMITTING and self._store.emitted_items(plan.plan_id):
            raise PlanTurnRefused(
                "emission has started; there is no rollback (ADR-0014) — "
                "re-run `plan emit` to finish, then close the children by hand "
                "if the plan is truly wrong"
            )
        if plan.status is PlanStatus.EMITTED:
            raise PlanTurnRefused("the plan is emitted; there is no rollback (ADR-0014)")
        intent_id = self._store.add_intent(
            intent_type="plan_reject",
            source=source,
            node_id=None,
            payload={"plan_id": plan.plan_id, "reason": reason},
            external_id=f"plan_reject:{plan.plan_id}",
            now=self._clock(),
        )
        consume_plan_intents(self._store, plan, clock=self._clock)
        return intent_id

    def emit(self, epic_key: str) -> EmitReport:
        """Consume pending plan intents, then run the idempotent emission.

        Re-runnable at any time; safe when nothing is pending. After a
        completed emission the re-run reports the existing children as a
        no-op rather than erroring."""
        latest = self._store.latest_plan_for_epic(self._provider(), epic_key)
        if latest is not None and latest.status is PlanStatus.EMITTED:
            report = EmitReport()
            report.complete = True
            report.child_keys = [
                entry.external_key for entry in self._store.emitted_items(latest.plan_id)
            ]
            return report
        plan = self._require_plan(epic_key)
        plan = consume_plan_intents(self._store, plan, clock=self._clock)
        if plan.status is PlanStatus.DISCARDED:
            report = EmitReport()
            report.failure = f"plan discarded: {plan.discard_reason}"
            return report
        if plan.status is not PlanStatus.EMITTING:
            raise PlanTurnRefused(
                f"plan is {plan.status.value}; approve it before emitting (ADR-0014)"
            )
        return run_emit(self._store, self._tracker, plan, clock=self._clock)

    # -- helpers -------------------------------------------------------------

    def _provider(self) -> str:
        return self._config.tracker.provider

    def _require_plan(self, epic_key: str) -> PlanRecord:
        plan = self._store.plan_for_epic(self._provider(), epic_key)
        if plan is None:
            raise UnknownEpic(f"no live plan for {epic_key}; start one with `ticketflow plan new`")
        return plan

    def _fetch_epic(self, epic_key: str) -> TrackerItem:
        items, _ = self._tracker.fetch_nodes(None)
        for item in items:
            if item.external_key == epic_key:
                return item
        raise UnknownEpic(f"the tracker has no item {epic_key}")

    def _upstream_handoffs(self, epic_key: str) -> dict[str, str]:
        """Direct upstream handoffs only — never transitive (ADR-0013)."""
        node_id = self._store.resolve_external(self._provider(), epic_key)
        if node_id is None:
            return {}
        handoffs = {}
        for upstream in self._store.upstreams_of(node_id):
            handoff = self._store.get_handoff(upstream)
            if handoff:
                handoffs[upstream] = handoff
        return handoffs

    def _check(self, proposed: Plan, plan: PlanRecord) -> None:
        """Identity and semantic validation for every turn's output."""
        if proposed.plan_id != plan.plan_id or proposed.epic_key != plan.epic_key:
            raise PlanValidationError("the turn changed the plan's identity")
        errors = semantic_errors(proposed)
        if errors:
            raise PlanValidationError("; ".join(errors))

    def _record_revision(self, plan: PlanRecord, proposed: Plan, *, source: str) -> int:
        yaml_text = dump_plan(proposed)
        revision = self._store.add_plan_revision(
            plan.plan_id, yaml_text=yaml_text, source=source, now=self._clock()
        )
        write_plan_file(self._config.plans_dir, plan.epic_key, yaml_text)
        return revision

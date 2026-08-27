"""Lifecycle tests for the reconcile tick (ADR-0008), through fakes only."""

from ticketflow.domain.model import NodeState
from ticketflow.orchestrator.core import branch_for, derive_node_id
from ticketflow.ports.codehost import CheckState, ReviewDecision
from ticketflow.ports.runner import AttemptStatus, FailureClass, PollResult

from .conftest import Harness


class TestSync:
    def test_new_items_become_blocked_nodes(self, h: Harness) -> None:
        h.add_item("#1", "First")
        h.add_item("#2", "Second", body="depends-on: #1")
        h.orchestrator.tick()
        node2 = h.store.get_node(h.node_id_for("#2"))
        assert node2 is not None
        assert h.store.upstreams_of(node2.node_id) == (h.node_id_for("#1"),)

    def test_closed_item_syncs_as_merged(self, h: Harness) -> None:
        h.add_item("#1", "Already done", closed=True)
        h.add_item("#2", "Next", body="depends-on: #1")
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.MERGED
        # And its dependent became ready and was dispatched.
        assert h.state_of("#2") is NodeState.IN_PROGRESS

    def test_unresolved_dependency_recorded(self, h: Harness) -> None:
        h.add_item("#1", "Depends on a ghost", body="depends-on: #99")
        h.orchestrator.tick()
        kinds = [e.kind for e in h.store.events_after(0)]
        assert "dependency_unresolved" in kinds

    def test_cycle_stops_scheduling_not_the_tick(self, h: Harness) -> None:
        h.add_item("#1", "A", body="depends-on: #2")
        h.add_item("#2", "B", body="depends-on: #1")
        report = h.orchestrator.tick()
        assert report.graph_ok is False
        assert report.dispatched == 0
        assert h.state_of("#1") is NodeState.BLOCKED

    def test_resync_updates_content(self, h: Harness) -> None:
        h.add_item("#1", "Old title")
        h.orchestrator.tick()
        h.tracker.items.clear()
        h.add_item("#1", "New title")
        h.orchestrator.tick()
        node = h.store.get_node(h.node_id_for("#1"))
        assert node is not None
        assert node.title == "New title"

    def test_node_id_is_deterministic(self, h: Harness) -> None:
        h.add_item("#1", "One")
        h.orchestrator.tick()
        assert h.node_id_for("#1") == derive_node_id("github", "#1")


class TestDispatch:
    def test_ready_node_is_leased_and_started(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        report = h.orchestrator.tick()
        assert report.dispatched == 1
        assert h.state_of("#1") is NodeState.IN_PROGRESS
        node_id = h.node_id_for("#1")
        assert h.store.get_lease(node_id) is not None
        assert len(h.runner.started) == 1
        started = h.runner.started[0]
        assert branch_for(node_id) in started.dispatch.prompt
        attempt = h.store.get_attempt(node_id, 1)
        assert attempt is not None
        assert attempt.pid is not None

    def test_max_parallel_caps_dispatch(self, h: Harness) -> None:
        for i in range(1, 5):
            h.add_item(f"#{i}", f"Work {i}")
        report = h.orchestrator.tick()
        assert report.dispatched == 2  # limits.max_parallel

    def test_yolo_flag_reaches_policy_and_event_log(self, h: Harness) -> None:
        from ticketflow.orchestrator.core import Orchestrator

        yolo_orch = Orchestrator(
            store=h.store,
            tracker=h.tracker,
            runner=h.runner,
            codehost=h.codehost,
            workspaces=h.workspaces,
            config=h.config,
            clock=h.clock,
            yolo=True,
        )
        h.add_item("#1", "Work")
        yolo_orch.tick()
        assert h.runner.started[0].policy.yolo is True
        dispatch_events = [e for e in h.store.events_after(0) if e.kind == "dispatched"]
        assert dispatch_events[0].payload["yolo"] is True


class TestHarvest:
    def _run_to_in_progress(self, h: Harness, key: str = "#1") -> str:
        h.add_item(key, "Work")
        h.orchestrator.tick()
        return h.node_id_for(key)

    def test_clean_exit_with_branch_opens_pr(self, h: Harness) -> None:
        node_id = self._run_to_in_progress(h)
        h.runner.script_exit(node_id, 1, exit_code=0)
        h.codehost.branches.add(branch_for(node_id))
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS
        assert len(h.codehost.opened) == 1

    def test_handoff_collected_and_posted(self, h: Harness) -> None:
        node_id = self._run_to_in_progress(h)
        workspace = h.workspaces.prepare(node_id, 1, bootstrap=False)
        (workspace / "handoff.md").write_text("Touched src/x. Gotcha: y.")
        h.runner.script_exit(node_id, 1, exit_code=0)
        h.codehost.branches.add(branch_for(node_id))
        h.orchestrator.tick()
        assert h.store.get_handoff(node_id) == "Touched src/x. Gotcha: y."
        assert any("Touched src/x" in text for _, text in h.codehost.comments)

    def test_clean_exit_empty_diff_escalates(self, h: Harness) -> None:
        node_id = self._run_to_in_progress(h)
        h.runner.script_exit(node_id, 1, exit_code=0)  # no branch pushed
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        node = h.store.get_node(node_id)
        assert node is not None
        assert node.blocked_reason is not None
        assert "empty diff" in node.blocked_reason

    def test_clean_exit_branch_pushed_but_empty_diff_escalates(self, h: Harness) -> None:
        # ADR-0010: exit code, then checks, then a NON-EMPTY diff — a pushed
        # branch identical to the default branch is still an empty diff.
        node_id = self._run_to_in_progress(h)
        h.runner.script_exit(node_id, 1, exit_code=0)
        h.codehost.branches.add(branch_for(node_id))
        h.workspaces.diff_stats[(node_id, 1)] = ""
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        assert len(h.codehost.opened) == 0

    def test_crash_retries_then_escalates(self, h: Harness) -> None:
        node_id = self._run_to_in_progress(h)
        for attempt in range(1, 4):
            h.runner.script_exit(node_id, attempt, exit_code=1)
            h.orchestrator.tick()
        # attempts 1 and 2 crash back to Ready and re-dispatch; the third
        # crash hits max_attempts=3 and escalates.
        assert h.state_of("#1") is NodeState.ESCALATED

    def test_timeout_cancels_and_escalates(self, h: Harness) -> None:
        node_id = self._run_to_in_progress(h)
        h.runner.script(node_id, 1, PollResult(status=AttemptStatus.TIMED_OUT))
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        assert len(h.runner.cancelled) == 1

    def test_finished_attempt_is_harvested_even_after_lease_expiry(self, h: Harness) -> None:
        # ADR-0010 adoption: "re-attach live ones, harvest finished ones,
        # expire the rest". An attempt that finished while the orchestrator
        # was down must be harvested on the next tick — never aborted and
        # redispatched.
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        h.runner.script_exit(node_id, 1, exit_code=0)
        h.codehost.branches.add(branch_for(node_id))
        h.clock.advance(h.config.limits.lease_ttl_seconds + 120)  # downtime
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS
        assert len(h.codehost.opened) == 1
        assert len(h.runner.started) == 1  # no redispatch

    def test_live_attempt_reattaches_after_downtime(self, h: Harness) -> None:
        # A still-running attempt found after downtime is re-attached (lease
        # renewed), not expired back to Ready.
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        h.clock.advance(h.config.limits.lease_ttl_seconds + 120)  # downtime
        h.orchestrator.tick()  # runner fake reports RUNNING
        assert h.state_of("#1") is NodeState.IN_PROGRESS
        assert len(h.runner.started) == 1

    def test_interrupted_harvest_resumes_on_next_tick(self, h: Harness) -> None:
        # A crash can land between "attempt observed exited" and the state
        # transition (e.g. a network error while opening the PR). The next
        # tick must resume the harvest from the terminal attempt row — the
        # node must never wedge in_progress with its work already done.
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        h.codehost.branches.add(branch_for(node_id))
        # Simulate the interrupted first observation: row already terminal,
        # lease already released, but the node state never advanced.
        h.store.update_attempt(
            node_id,
            1,
            status="exited",
            exit_code=0,
            session_id="sess-1",
            finished_at=h.clock(),
        )
        h.store.release_lease(node_id)
        h.runner.script_exit(node_id, 1, exit_code=0)  # cached re-poll result
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS
        assert len(h.codehost.opened) == 1
        assert len(h.runner.started) == 1  # resumed, not redispatched

    def test_lease_expiry_backstop_escalates_unpollable_nodes_when_repeated(
        self, h: Harness
    ) -> None:
        # ADR-0006: repeated lease expiry escalates. With poll-first
        # reconciliation a live process renews its lease, so the backstop
        # covers attempts that cannot be polled — a process that keeps dying
        # before recording anything (simulated by retiring the row).
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        for round_no in range(1, h.config.limits.max_attempts + 1):
            node = h.store.get_node(node_id)
            assert node is not None
            h.store.update_attempt(
                node_id, node.attempt_count, status="aborted", finished_at=h.clock()
            )
            h.clock.advance(h.config.limits.lease_ttl_seconds + 60)
            h.orchestrator.tick()
            if round_no < h.config.limits.max_attempts:
                assert h.state_of("#1") is NodeState.IN_PROGRESS, round_no  # re-dispatched
        assert h.state_of("#1") is NodeState.ESCALATED
        node = h.store.get_node(node_id)
        assert node is not None
        assert node.blocked_reason is not None
        assert "lease" in node.blocked_reason

    def test_quota_failure_pauses_dispatch(self, h: Harness) -> None:
        node_id = self._run_to_in_progress(h)
        h.add_item("#2", "More work")
        h.runner.script(
            node_id,
            1,
            PollResult(status=AttemptStatus.EXITED, exit_code=1, failure_class=FailureClass.QUOTA),
        )
        report = h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        assert report.dispatched == 0  # #2 was ready but dispatch is paused
        assert any("paused" in n for n in report.notes)
        # A global resume intent clears the pause.
        h.store.add_intent(intent_type="resume", source="cli", now=h.clock())
        report = h.orchestrator.tick()
        assert report.dispatched == 1

    def test_bootstrap_completes_on_push(self, h: Harness) -> None:
        h.codehost.exists = False
        h.add_item("#1", "Create the repo")
        h.orchestrator.tick()
        assert h.workspaces.bootstrap_requests == [(h.node_id_for("#1"), 1)]
        assert "does not exist yet" in h.runner.started[0].dispatch.prompt
        h.codehost.exists = True  # the agent created and pushed it
        h.runner.script_exit(h.node_id_for("#1"), 1, exit_code=0)
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.MERGED


class TestSettle:
    def _run_to_awaiting(self, h: Harness, key: str = "#1") -> tuple[str, int]:
        h.add_item(key, "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for(key)
        h.runner.script_exit(node_id, 1, exit_code=0)
        h.codehost.branches.add(branch_for(node_id))
        h.orchestrator.tick()
        pr = h.codehost.find_pr_for_branch(branch_for(node_id))
        assert pr is not None
        return node_id, pr

    def test_green_no_gates_merges_immediately(self, h: Harness) -> None:
        node_id, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS})
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.MERGED
        assert h.codehost.merged == [pr]
        # ADR-0009: the merge and which checks reported are evented.
        merged_events = [e for e in h.store.events_after(0) if e.kind == "merged"]
        assert len(merged_events) == 1
        assert merged_events[0].node_id == node_id
        assert merged_events[0].payload["pr"] == pr
        assert merged_events[0].payload["how"] == "ticketflow"
        assert merged_events[0].payload["checks"] == [{"name": "ci", "state": "success"}]

    def test_host_side_merge_is_evented_too(self, h: Harness) -> None:
        _, pr = self._run_to_awaiting(h)
        h.set_pr(pr, state="merged")
        h.orchestrator.tick()
        merged_events = [e for e in h.store.events_after(0) if e.kind == "merged"]
        assert merged_events[0].payload["how"] == "host"

    def test_pending_checks_wait(self, h: Harness) -> None:
        _, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.PENDING})
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS
        assert h.codehost.merged == []

    def test_failed_check_reruns_once_before_feedback(self, h: Harness) -> None:
        _, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.FAILURE})
        h.orchestrator.tick()
        assert h.codehost.reruns == [pr]
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS  # not yet feedback
        # Check passes after the re-run: recorded as a flake, then merged.
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS})
        h.orchestrator.tick()
        assert h.store.flake_rate("ci") == 1.0
        assert h.state_of("#1") is NodeState.MERGED

    def test_persistent_failure_dispatches_feedback(self, h: Harness) -> None:
        node_id, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.FAILURE})
        h.orchestrator.tick()  # re-run consumed
        h.orchestrator.tick()  # still red: feedback cycle
        assert h.state_of("#1") is NodeState.ADDRESSING_FEEDBACK
        assert len(h.runner.resumed) == 1
        _, feedback = h.runner.resumed[0]
        assert "ci" in feedback
        assert h.store.flake_rate("ci") == 0.0
        # The agent pushes a fix and exits; back to AwaitingSignals; goes green.
        h.runner.script_exit(node_id, 2, exit_code=0)
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS})
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.MERGED

    def test_unresolved_threads_block_merge_and_carry_comments(self, h: Harness) -> None:
        from ticketflow.ports.codehost import ReviewComment

        _, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS}, threads=1)
        h.codehost.prs[pr].feedback.append(
            ReviewComment(thread_id="t1", author="reviewer", body="Rename this.", path="a.py")
        )
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ADDRESSING_FEEDBACK
        _, feedback = h.runner.resumed[0]
        assert "Rename this." in feedback

    def test_cycle_cap_escalates(self, h: Harness) -> None:
        h.config.limits.cycle_cap = 1
        node_id, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.FAILURE})
        h.orchestrator.tick()  # rerun
        h.orchestrator.tick()  # cycle 1: feedback dispatch
        h.runner.script_exit(node_id, 2, exit_code=0)
        h.orchestrator.tick()  # back to awaiting
        h.orchestrator.tick()  # rerun for cycle 2's failure window
        h.orchestrator.tick()  # cycle 2 > cap: escalate
        assert h.state_of("#1") is NodeState.ESCALATED

    def test_merge_conflict_gets_one_narrow_redispatch_then_escalates(self, h: Harness) -> None:
        # ADR-0008 / spec §12.1: conflict resolution is capped at ONE attempt,
        # distinct from the feedback cycle cap — this is where agents silently
        # discard other people's work.
        node_id, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS}, mergeable=False)
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ADDRESSING_FEEDBACK
        assert len(h.runner.resumed) == 1
        _, feedback = h.runner.resumed[0]
        assert "conflict" in feedback.lower()
        h.runner.script_exit(node_id, 2, exit_code=0)
        h.orchestrator.tick()  # back to AwaitingSignals
        h.orchestrator.tick()  # still conflicting: escalate, no second attempt
        assert h.state_of("#1") is NodeState.ESCALATED
        assert len(h.runner.resumed) == 1

    def test_conflict_resolved_after_redispatch_merges(self, h: Harness) -> None:
        node_id, pr = self._run_to_awaiting(h)
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS}, mergeable=False)
        h.orchestrator.tick()
        h.runner.script_exit(node_id, 2, exit_code=0)
        # After the rebase push the host reports mergeable=None while it
        # recomputes; the settle must NOT escalate — it proceeds down the
        # ladder and lets the host arbitrate the merge attempt.
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS}, mergeable=None)
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.MERGED

    def test_approvals_missing_sets_auto_merge(self, h: Harness) -> None:
        _, pr = self._run_to_awaiting(h)
        h.codehost.merge_result = False
        h.codehost.auto_merge_result = True
        h.set_pr(pr, checks={"ci": CheckState.SUCCESS}, decision=ReviewDecision.REVIEW_REQUIRED)
        h.orchestrator.tick()
        assert h.codehost.auto_merged == [pr]
        assert h.state_of("#1") is NodeState.AWAITING_SIGNALS

    def test_externally_merged_pr_is_recognized(self, h: Harness) -> None:
        _, pr = self._run_to_awaiting(h)
        h.set_pr(pr, state="merged")
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.MERGED


class TestIntents:
    def test_cancel_running_node(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        h.store.add_intent(intent_type="cancel", source="cli", node_id=node_id, now=h.clock())
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        assert len(h.runner.cancelled) == 1

    def test_retry_escalated_with_feedback(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        h.runner.script_exit(node_id, 1, exit_code=0)  # empty diff -> escalate
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        h.store.add_intent(
            intent_type="retry",
            source="cli",
            node_id=node_id,
            payload={"feedback": "The acceptance criteria mean X, not Y."},
            now=h.clock(),
        )
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.IN_PROGRESS
        node = h.store.get_node(node_id)
        assert node is not None
        # Attempt numbering is monotonic — never reset — so run dirs and the
        # (node, attempt) idempotency key (ADR-0008) are never reused. What a
        # retry resets is the failure budgets (crash/cycle counters).
        assert node.attempt_count == 2
        assert node.crash_count == 0
        prompt = h.runner.started[-1].dispatch.prompt
        assert "The acceptance criteria mean X, not Y." in prompt

    def test_retry_resets_the_crash_budget_not_the_numbering(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        for attempt in range(1, 4):
            h.runner.script_exit(node_id, attempt, exit_code=1)
            h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED  # crash budget spent
        h.store.add_intent(intent_type="retry", source="cli", node_id=node_id, now=h.clock())
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.IN_PROGRESS
        node = h.store.get_node(node_id)
        assert node is not None
        assert node.attempt_count == 4  # continues, no collision with rows 1-3
        # One more crash must NOT immediately re-escalate: the budget is fresh.
        h.runner.script_exit(node_id, 4, exit_code=1)
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.IN_PROGRESS  # crashed once, retried

    def test_unblock_refused_under_escalated_ancestor(self, h: Harness) -> None:
        # ADR-0006: never let dependents of an Escalated node proceed — an
        # unblock intent must not bypass that rule.
        h.add_item("#1", "Base")
        h.add_item("#2", "On top", body="depends-on: #1")
        h.orchestrator.tick()
        h.runner.script_exit(h.node_id_for("#1"), 1, exit_code=0)  # empty diff
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.ESCALATED
        h.store.add_intent(
            intent_type="unblock", source="cli", node_id=h.node_id_for("#2"), now=h.clock()
        )
        h.orchestrator.tick()
        assert h.state_of("#2") is NodeState.BLOCKED
        kinds = [e.kind for e in h.store.events_after(0)]
        assert "intent_unhandled" in kinds

    def test_unblock_refused_while_upstream_unfinished(self, h: Harness) -> None:
        # ADR-0006: Blocked -> Ready fires only when all upstream edges are
        # resolved; unblock enforces the SAME guard, so it cannot start a
        # node on top of unfinished upstream work.
        h.add_item("#1", "Base")
        h.add_item("#2", "On top", body="depends-on: #1")
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.IN_PROGRESS
        h.store.add_intent(
            intent_type="unblock", source="cli", node_id=h.node_id_for("#2"), now=h.clock()
        )
        h.orchestrator.tick()
        assert h.state_of("#2") is NodeState.BLOCKED

    def test_unblock_overrides_unresolved_external_dependency(self, h: Harness) -> None:
        # The legitimate use: a dependency key that resolves to nothing (typo,
        # foreign project) keeps a node Blocked; a human may override that.
        h.add_item("#1", "Waits on a ghost", body="depends-on: #99")
        h.orchestrator.tick()
        assert h.state_of("#1") is NodeState.BLOCKED
        h.store.add_intent(
            intent_type="unblock", source="cli", node_id=h.node_id_for("#1"), now=h.clock()
        )
        h.orchestrator.tick()
        # Applied on the next tick's intent step, then dispatched immediately.
        assert h.state_of("#1") is NodeState.IN_PROGRESS

    def test_unknown_intent_recorded_not_crashing(self, h: Harness) -> None:
        h.store.add_intent(intent_type="approve-plan", source="cli", now=h.clock())
        h.orchestrator.tick()
        kinds = [e.kind for e in h.store.events_after(0)]
        assert "intent_unhandled" in kinds

    def test_tracker_intents_are_ingested_idempotently(self, h: Harness) -> None:
        from ticketflow.ports.tracker import TrackerIntent

        h.add_item("#1", "Work")
        h.tracker.intents.append(
            TrackerIntent(external_id="gh:evt-1", intent_type="cancel", external_key="#1")
        )
        h.orchestrator.tick()
        h.orchestrator.tick()  # same intent fetched again; must not reapply
        cancels = [
            e
            for e in h.store.events_after(0)
            if e.kind == "state_changed" and e.payload.get("to") == "escalated"
        ]
        assert len(cancels) == 1


class TestProjectionAndHalt:
    def test_states_are_projected_to_the_tracker(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        assert ("#1", NodeState.READY) in h.tracker.pushed_states
        assert ("#1", NodeState.IN_PROGRESS) in h.tracker.pushed_states

    def test_escalation_comment_is_posted(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        h.runner.script_exit(h.node_id_for("#1"), 1, exit_code=0)
        h.orchestrator.tick()
        assert any("Escalated" in text for _, text in h.tracker.comments)

    def test_dependents_of_escalated_show_root_cause(self, h: Harness) -> None:
        h.add_item("#1", "Base")
        h.add_item("#2", "On top", body="depends-on: #1")
        h.orchestrator.tick()
        node1 = h.node_id_for("#1")
        h.runner.script_exit(node1, 1, exit_code=0)  # empty diff -> escalated
        h.orchestrator.tick()
        node2 = h.store.get_node(h.node_id_for("#2"))
        assert node2 is not None
        assert node2.blocked_reason == f"blocked by escalated {node1}"

    def test_halt_after_idle_ticks_with_escalations(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        h.orchestrator.tick()
        h.runner.script_exit(h.node_id_for("#1"), 1, exit_code=0)
        h.orchestrator.tick()  # escalates (empty diff)
        reports = [h.orchestrator.tick() for _ in range(3)]  # halt_ticks=3
        assert reports[-1].halted is True

    def test_no_halt_while_work_flows(self, h: Harness) -> None:
        h.add_item("#1", "Work")
        report = h.orchestrator.tick()
        assert report.halted is False


class TestParseIssueReporting:
    def test_malformed_body_gets_event_and_comment_once(self, h: Harness) -> None:
        # ADR-0007: malformed blocks are reported — an event AND an issue
        # comment (the teaching mechanism) — and never repeated per sync.
        h.add_item("#1", "Bad deps", body="depends-on: not a key!")
        h.orchestrator.tick()
        h.orchestrator.tick()  # same content re-fetched: no repeat
        comments = [text for _, text in h.tracker.comments if "could not fully parse" in text]
        assert len(comments) == 1
        assert "not a key!" in comments[0]
        events = [e for e in h.store.events_after(0) if e.kind == "body_parse_issue"]
        assert len(events) == 1


class TestScopeRecord:
    def test_declared_vs_actual_paths_recorded_per_attempt(self, h: Harness) -> None:
        # ADR-0007: the record that decides whether scope hints survive.
        h.add_item("#1", "Scoped", body="scope: src/widget.py, docs/")
        h.orchestrator.tick()
        node_id = h.node_id_for("#1")
        dispatched = [e for e in h.store.events_after(0) if e.kind == "dispatched"]
        assert dispatched[0].payload["scope_declared"] == ["src/widget.py", "docs/"]
        h.runner.script_exit(node_id, 1, exit_code=0)
        h.codehost.branches.add(branch_for(node_id))
        h.orchestrator.tick()
        observed = [e for e in h.store.events_after(0) if e.kind == "scope_observed"]
        assert len(observed) == 1
        assert observed[0].attempt == 1
        assert observed[0].payload["declared"] == ["src/widget.py", "docs/"]
        assert observed[0].payload["actual"] == ["src/widget.py"]

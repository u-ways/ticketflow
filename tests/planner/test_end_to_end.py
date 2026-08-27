"""The whole ADR-0014 contract in one narrative: ground → synthesize →
revise (prune) → approve (diff logged) → emit with a crash → children held →
resume emit → children adopted by ordinary sync and scheduled per the DAG."""

from pathlib import Path

from tests.fakes import FakeCodeHost, FakeRunner, FakeSynthesizer, FakeTracker
from tests.orchestrator.conftest import FakeClock, FakeWorkspaces
from ticketflow.config import CodeHostConfig, Config, Limits, PlannerConfig, TrackerConfig
from ticketflow.domain.model import NodeState
from ticketflow.domain.plan import PlanStatus
from ticketflow.orchestrator.core import Orchestrator
from ticketflow.planner.grounding import grounding_workspace_id
from ticketflow.planner.schema import Plan, PlanEdge, PlanItem
from ticketflow.planner.service import Planner
from ticketflow.planner.yaml_io import plan_filename
from ticketflow.ports.tracker import TrackerItem
from ticketflow.store.store import Store


def proposal(plan_id: str, *, pruned: bool = False) -> Plan:
    """Four items; 0→1→2 evidenced, 0→3 proposed without evidence."""
    return Plan(
        plan_id=plan_id,
        epic_key="#42",
        items=(
            PlanItem(index=0, title="Scaffold", body="Create the package."),
            PlanItem(index=1, title="Feature", body="Build the feature."),
            PlanItem(index=2, title="CLI", body="Wrap it in a CLI."),
            PlanItem(index=3, title="Docs", body="Write the docs."),
        ),
        edges=(
            PlanEdge(upstream=0, downstream=1, confidence=0.9, evidence="imports"),
            PlanEdge(upstream=1, downstream=2, confidence=0.8, evidence="CLI wraps feature"),
        ),
        unevidenced_edges=() if pruned else (PlanEdge(upstream=0, downstream=3, confidence=0.3),),
    )


def test_plan_to_scheduled_children(tmp_path: Path) -> None:
    config = Config(
        tracker=TrackerConfig(provider="github", repo="u-ways/qa"),
        codehost=CodeHostConfig(repo="u-ways/qa"),
        limits=Limits(max_parallel=2),
        planner=PlannerConfig(synthesis_model="test-model", poll_interval_seconds=1.0),
        state_dir=tmp_path / ".ticketflow",
        plans_dir=tmp_path / "plans",
    )
    store = Store.open(tmp_path / "tf.db")
    tracker = FakeTracker()
    # The epic is closed: planning happens on a done epic whose children are
    # the actual work.
    tracker.items.append(
        TrackerItem(
            provider="github", external_key="#42", title="Epic", body="Make it so.", closed=True
        )
    )
    runner = FakeRunner()
    synthesizer = FakeSynthesizer()
    workspaces = FakeWorkspaces(tmp_path / "ws")
    clock = FakeClock()
    planner = Planner(
        store=store,
        tracker=tracker,
        runner=runner,
        synthesizer=synthesizer,
        workspaces=workspaces,
        config=config,
        repo_exists=lambda: True,
        clock=clock,
        sleep=lambda seconds: clock.advance(int(seconds)),
    )
    orchestrator = Orchestrator(
        store=store,
        tracker=tracker,
        runner=runner,
        codehost=FakeCodeHost(),
        workspaces=workspaces,
        config=config,
        clock=clock,
    )

    # -- ground + synthesize -------------------------------------------------
    record = planner.ingest("#42")
    plan_id = record.plan_id
    ws = workspaces.root / grounding_workspace_id(plan_id) / "1"
    ws.mkdir(parents=True)
    (ws / "brief.md").write_text("# Brief\nSeams found.", encoding="utf-8")
    runner.script_exit(grounding_workspace_id(plan_id), 1)
    synthesizer.script(proposal(plan_id))
    plan = planner.new("#42")
    assert plan.status is PlanStatus.IN_REVIEW
    assert (config.plans_dir / plan_filename("#42")).is_file()

    # -- revise: the reviewer prunes the unevidenced edge --------------------
    synthesizer.script(proposal(plan_id, pruned=True))
    assert planner.revise("#42", "drop the docs edge; docs can go in parallel") == 2

    # -- approve: the intent pins revision 2; the diff is the dataset --------
    assert planner.request_approval("#42") is not None
    # (an orchestrator ticking now must not swallow the approval)
    orchestrator.tick()
    assert any(i.intent_type == "plan_approve" for i in store.unprocessed_intents())

    # -- emit, crashing after two creates ------------------------------------
    tracker.fail_after_creates = 2
    report = planner.emit("#42")
    assert not report.complete
    assert store.plan_status(plan_id) is PlanStatus.EMITTING  # no rollback
    [diff_event] = [e for e in store.events_after(0) if e.kind == "plan_approved"]
    assert diff_event.payload["edges_removed"] == [
        {"upstream": 0, "downstream": 3, "confidence": 0.3}
    ]
    assert any("could not finish" in text for key, text in tracker.comments if key == "#42")

    # -- the partials sync as held nodes, never dispatchable -----------------
    orchestrator.tick()
    orchestrator.tick()
    held = [
        node
        for node in store.list_nodes(state=NodeState.BLOCKED)
        if node.blocked_reason and "awaiting plan emission" in node.blocked_reason
    ]
    assert len(held) == 2  # the two created children, tagged and held
    # Nothing dispatched anywhere: the only runner start was grounding.
    assert all(s.dispatch.node_id.startswith("plan-") for s in runner.started)

    # -- resume emit: adopts the partials, finishes items, edges, mirrors ----
    tracker.fail_after_creates = None
    retry = planner.emit("#42")
    assert retry.complete
    assert len(tracker.created) == 4  # never duplicated
    assert store.plan_status(plan_id) is PlanStatus.EMITTED

    # -- ordinary sync releases the hold; the DAG schedules -------------------
    orchestrator.tick()
    key_for = dict(zip([i.index for i in proposal(plan_id).items], retry.child_keys, strict=True))
    state_of: dict[str, NodeState] = {}
    for key in key_for.values():
        node_id = store.resolve_external("github", key)
        assert node_id is not None
        node = store.get_node(node_id)
        assert node is not None
        state_of[key] = node.state
    # Roots (Scaffold, Docs — its proposed edge was pruned) dispatch first;
    # dependents wait on their upstream edges exactly like human tickets.
    assert state_of[key_for[0]] is NodeState.IN_PROGRESS
    assert state_of[key_for[3]] is NodeState.IN_PROGRESS
    assert state_of[key_for[1]] is NodeState.BLOCKED
    assert state_of[key_for[2]] is NodeState.BLOCKED

    # The emitted bodies are the canonical graph: depends-on lines carry the
    # real tracker keys (ADR-0007).
    feature_node = store.resolve_external("github", key_for[1])
    assert feature_node is not None
    assert store.upstreams_of(feature_node) == (store.resolve_external("github", key_for[0]),)

    # The canary stays honest: an unknown hyphenated type is still unhandled.
    store.add_intent(intent_type="approve-plan", source="cli", now=clock())
    orchestrator.tick()
    assert any(e.kind == "intent_unhandled" for e in store.events_after(0))

    store.close()


def test_replan_after_rejection_gets_a_fresh_identity(tmp_path: Path) -> None:
    config = Config(
        tracker=TrackerConfig(provider="github", repo="u-ways/qa"),
        codehost=CodeHostConfig(repo="u-ways/qa"),
        planner=PlannerConfig(synthesis_model="test-model", poll_interval_seconds=1.0),
        state_dir=tmp_path / ".ticketflow",
        plans_dir=tmp_path / "plans",
    )
    store = Store.open(tmp_path / "tf.db")
    tracker = FakeTracker()
    tracker.items.append(
        TrackerItem(provider="github", external_key="#42", title="Epic", body="Do.")
    )
    clock = FakeClock()
    planner = Planner(
        store=store,
        tracker=tracker,
        runner=FakeRunner(),
        synthesizer=FakeSynthesizer(),
        workspaces=FakeWorkspaces(tmp_path / "ws"),
        config=config,
        repo_exists=lambda: True,
        clock=clock,
        sleep=lambda seconds: clock.advance(int(seconds)),
    )

    first = planner.ingest("#42")
    planner.request_rejection("#42", "wrong shape")
    assert store.plan_status(first.plan_id) is PlanStatus.DISCARDED

    second = planner.ingest("#42")
    # Distinct identity: the discarded plan's markers and idempotency keys
    # can never be adopted by the re-plan (ADR-0014).
    assert second.plan_id != first.plan_id
    store.close()

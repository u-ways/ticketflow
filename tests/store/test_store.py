"""SQLite is the canonical state store (ADR-0003).

One writer, WAL mode, plain sequential migrations. State changes go through the
transition table and are evented atomically (ADR-0005, ADR-0006).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ticketflow.domain.errors import IllegalTransition, UnknownNode
from ticketflow.domain.model import NodeState
from ticketflow.store.store import Store

T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "ticketflow.db")
    yield s
    s.close()


def make_node(store: Store, node_id: str = "n1", state: NodeState = NodeState.BLOCKED) -> str:
    store.insert_node(node_id=node_id, title=f"Node {node_id}", body="", state=state, now=T0)
    return node_id


class TestMigrations:
    def test_open_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "tf.db"
        s1 = Store.open(path)
        s1.close()
        s2 = Store.open(path)  # re-applying migrations must be a no-op
        assert s2.schema_version() >= 1
        s2.close()

    def test_wal_mode(self, store: Store) -> None:
        assert store.journal_mode() == "wal"


class TestNodes:
    def test_insert_and_get_roundtrip(self, store: Store) -> None:
        store.insert_node(
            node_id="n1",
            title="Build the API",
            body="depends-on: X-1",
            state=NodeState.BLOCKED,
            scope_hints=("src/api/",),
            now=T0,
        )
        node = store.get_node("n1")
        assert node is not None
        assert node.title == "Build the API"
        assert node.state is NodeState.BLOCKED
        assert node.scope_hints == ("src/api/",)
        assert node.attempt_count == 0
        assert node.cycle_count == 0

    def test_get_missing_returns_none(self, store: Store) -> None:
        assert store.get_node("ghost") is None

    def test_list_nodes_filtered_by_state(self, store: Store) -> None:
        make_node(store, "a", NodeState.BLOCKED)
        make_node(store, "b", NodeState.READY)
        make_node(store, "c", NodeState.READY)
        assert {n.node_id for n in store.list_nodes(state=NodeState.READY)} == {"b", "c"}
        assert len(store.list_nodes()) == 3

    def test_update_content_preserves_state(self, store: Store) -> None:
        make_node(store, "n1", NodeState.READY)
        store.update_node_content(
            "n1", title="New title", body="new body", scope_hints=("x/",), now=at(5)
        )
        node = store.get_node("n1")
        assert node is not None
        assert node.title == "New title"
        assert node.state is NodeState.READY

    def test_legal_state_change_applies_and_events(self, store: Store) -> None:
        make_node(store, "n1", NodeState.BLOCKED)
        store.set_state("n1", NodeState.READY, now=at(1))
        node = store.get_node("n1")
        assert node is not None
        assert node.state is NodeState.READY
        kinds = [e.kind for e in store.events_after(0)]
        assert "state_changed" in kinds

    def test_illegal_state_change_raises_and_changes_nothing(self, store: Store) -> None:
        make_node(store, "n1", NodeState.BLOCKED)
        events_before = len(store.events_after(0))
        with pytest.raises(IllegalTransition):
            store.set_state("n1", NodeState.MERGED, now=at(1))
        node = store.get_node("n1")
        assert node is not None
        assert node.state is NodeState.BLOCKED
        assert len(store.events_after(0)) == events_before

    def test_set_state_on_unknown_node_raises(self, store: Store) -> None:
        with pytest.raises(UnknownNode):
            store.set_state("ghost", NodeState.READY, now=T0)

    def test_escalation_reason_recorded(self, store: Store) -> None:
        make_node(store, "n1", NodeState.READY)
        store.set_state("n1", NodeState.IN_PROGRESS, now=at(1))
        store.set_state("n1", NodeState.ESCALATED, reason="wall-clock timeout", now=at(2))
        node = store.get_node("n1")
        assert node is not None
        assert node.blocked_reason == "wall-clock timeout"

    def test_counters(self, store: Store) -> None:
        make_node(store, "n1")
        assert store.bump_attempt_count("n1", now=at(1)) == 1
        assert store.bump_attempt_count("n1", now=at(2)) == 2
        assert store.bump_cycle_count("n1", now=at(3)) == 1
        store.reset_counters("n1", now=at(4))
        node = store.get_node("n1")
        assert node is not None
        assert node.attempt_count == 0
        assert node.cycle_count == 0


class TestExternalRefs:
    def test_link_and_resolve(self, store: Store) -> None:
        make_node(store, "n1")
        store.link_external("n1", provider="jira", external_key="PROJ-41", etag="v7")
        assert store.resolve_external("jira", "PROJ-41") == "n1"
        assert store.resolve_external("jira", "PROJ-999") is None

    def test_one_node_many_refs(self, store: Store) -> None:
        make_node(store, "n1")
        store.link_external("n1", provider="jira", external_key="PROJ-41")
        store.link_external("n1", provider="github", external_key="#12")
        refs = store.refs_for("n1")
        assert {(r.provider, r.external_key) for r in refs} == {
            ("jira", "PROJ-41"),
            ("github", "#12"),
        }

    def test_relink_same_key_updates_etag(self, store: Store) -> None:
        make_node(store, "n1")
        store.link_external("n1", provider="jira", external_key="PROJ-41", etag="v1")
        store.link_external("n1", provider="jira", external_key="PROJ-41", etag="v2")
        refs = store.refs_for("n1")
        assert len(refs) == 1
        assert refs[0].etag == "v2"


class TestEdges:
    def test_replace_upstreams(self, store: Store) -> None:
        for n in ("a", "b", "c"):
            make_node(store, n)
        store.replace_upstreams("c", ["a", "b"])
        assert set(store.upstreams_of("c")) == {"a", "b"}
        assert store.downstreams_of("a") == ("c",)
        store.replace_upstreams("c", ["a"])
        assert set(store.upstreams_of("c")) == {"a"}

    def test_all_edges(self, store: Store) -> None:
        for n in ("a", "b"):
            make_node(store, n)
        store.replace_upstreams("b", ["a"])
        assert store.all_edges() == {("a", "b")}


class TestLeases:
    def test_claim_is_exclusive_until_expiry(self, store: Store) -> None:
        make_node(store, "n1")
        assert store.claim_lease("n1", worker_id="w1", attempt=1, ttl_seconds=60, now=T0)
        assert not store.claim_lease("n1", worker_id="w2", attempt=1, ttl_seconds=60, now=at(30))

    def test_claim_succeeds_after_expiry(self, store: Store) -> None:
        make_node(store, "n1")
        assert store.claim_lease("n1", worker_id="w1", attempt=1, ttl_seconds=60, now=T0)
        assert store.claim_lease("n1", worker_id="w2", attempt=2, ttl_seconds=60, now=at(61))

    def test_extend_lease_moves_expiry(self, store: Store) -> None:
        make_node(store, "n1")
        store.claim_lease("n1", worker_id="w1", attempt=1, ttl_seconds=60, now=T0)
        store.extend_lease("n1", ttl_seconds=60, now=at(50))
        # Would have expired at T0+60; the extension keeps it alive.
        assert not store.claim_lease("n1", worker_id="w2", attempt=1, ttl_seconds=60, now=at(70))

    def test_expire_stale_returns_and_removes(self, store: Store) -> None:
        make_node(store, "n1")
        make_node(store, "n2")
        store.claim_lease("n1", worker_id="w1", attempt=1, ttl_seconds=10, now=T0)
        store.claim_lease("n2", worker_id="w1", attempt=1, ttl_seconds=600, now=T0)
        expired = store.expire_stale_leases(now=at(60))
        assert expired == ("n1",)
        assert store.get_lease("n1") is None
        assert store.get_lease("n2") is not None

    def test_release(self, store: Store) -> None:
        make_node(store, "n1")
        store.claim_lease("n1", worker_id="w1", attempt=1, ttl_seconds=60, now=T0)
        store.release_lease("n1")
        assert store.get_lease("n1") is None


class TestAttempts:
    def test_create_is_idempotent(self, store: Store) -> None:
        make_node(store, "n1")
        assert store.create_attempt("n1", attempt=1, runner="claude", run_dir="runs/n1/1", now=T0)
        assert not store.create_attempt(
            "n1", attempt=1, runner="claude", run_dir="runs/n1/1", now=at(1)
        )
        assert store.get_attempt("n1", 1) is not None

    def test_update_and_running_listing(self, store: Store) -> None:
        make_node(store, "n1")
        store.create_attempt("n1", attempt=1, runner="claude", run_dir="runs/n1/1", now=T0)
        store.update_attempt("n1", 1, pid=4242, create_time=123.5, session_id="s-1")
        running = store.running_attempts()
        assert [(a.node_id, a.attempt, a.pid) for a in running] == [("n1", 1, 4242)]
        store.update_attempt("n1", 1, status="exited", exit_code=0, finished_at=at(60))
        assert store.running_attempts() == []
        attempt = store.get_attempt("n1", 1)
        assert attempt is not None
        assert attempt.exit_code == 0
        assert attempt.session_id == "s-1"


class TestIntents:
    def test_add_and_consume(self, store: Store) -> None:
        make_node(store, "n1")
        intent_id = store.add_intent(
            intent_type="retry", source="cli", node_id="n1", payload={"note": "go"}, now=T0
        )
        assert intent_id is not None
        pending = store.unprocessed_intents()
        assert len(pending) == 1
        assert pending[0].intent_type == "retry"
        assert pending[0].payload == {"note": "go"}
        store.mark_intent_processed(intent_id, now=at(1))
        assert store.unprocessed_intents() == []

    def test_external_id_makes_ingestion_idempotent(self, store: Store) -> None:
        first = store.add_intent(
            intent_type="approve", source="jira", external_id="jira:evt-1", now=T0
        )
        dup = store.add_intent(
            intent_type="approve", source="jira", external_id="jira:evt-1", now=at(1)
        )
        assert first is not None
        assert dup is None
        assert len(store.unprocessed_intents()) == 1

    def test_mark_processed_is_idempotent(self, store: Store) -> None:
        intent_id = store.add_intent(intent_type="resume", source="cli", now=T0)
        assert intent_id is not None
        store.mark_intent_processed(intent_id, now=at(1))
        store.mark_intent_processed(intent_id, now=at(2))  # no-op, no raise
        assert store.unprocessed_intents() == []

    def test_unknown_intent_types_are_stored_not_rejected(self, store: Store) -> None:
        # ADR-0014: intent handling must not close off new types.
        intent_id = store.add_intent(intent_type="approve-plan", source="cli", now=T0)
        assert intent_id is not None
        assert store.unprocessed_intents()[0].intent_type == "approve-plan"


class TestEvents:
    def test_append_and_cursor_read(self, store: Store) -> None:
        e1 = store.append_event("dispatched", now=T0, node_id="n1", attempt=1)
        e2 = store.append_event("merged", now=at(5), node_id="n1", payload={"pr": 7})
        assert e2 > e1
        events = store.events_after(0)
        assert [e.kind for e in events] == ["dispatched", "merged"]
        assert store.events_after(e1)[0].kind == "merged"
        assert events[1].payload == {"pr": 7}

    def test_store_exposes_no_event_mutation(self, store: Store) -> None:
        # Append-only (ADR-0005): the API surface must offer no update/delete.
        forbidden = [n for n in dir(store) if "event" in n and ("delete" in n or "update" in n)]
        assert forbidden == []


class TestHandoffs:
    def test_set_and_get(self, store: Store) -> None:
        make_node(store, "n1")
        store.set_handoff("n1", "Touched src/api. Introduced FooPort.", now=T0)
        assert store.get_handoff("n1") == "Touched src/api. Introduced FooPort."
        assert store.get_handoff("nope") is None

    def test_overwrite_keeps_latest(self, store: Store) -> None:
        make_node(store, "n1")
        store.set_handoff("n1", "v1", now=T0)
        store.set_handoff("n1", "v2", now=at(1))
        assert store.get_handoff("n1") == "v2"


class TestCheckStats:
    def test_flake_rate(self, store: Store) -> None:
        store.record_check_outcome("pytest", flaked=False)
        store.record_check_outcome("pytest", flaked=True)
        store.record_check_outcome("pytest", flaked=False)
        store.record_check_outcome("pytest", flaked=False)
        assert store.flake_rate("pytest") == pytest.approx(0.25)
        assert store.flake_rate("unknown-check") == 0.0


class TestAtomicity:
    def test_state_change_and_event_commit_or_roll_back_together(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR-0005: the state row and its event are one unit of work. If the
        # event insert dies, the state change must roll back too.
        make_node(store, "n1", NodeState.BLOCKED)

        def boom(**_kwargs: object) -> int:
            raise RuntimeError("event insert failed")

        monkeypatch.setattr(store, "_append_event_row", boom)
        with pytest.raises(RuntimeError):
            store.set_state("n1", NodeState.READY, now=at(1))
        node = store.get_node("n1")
        assert node is not None
        assert node.state is NodeState.BLOCKED  # rolled back, not half-applied

    def test_state_changed_event_can_carry_the_attempt(self, store: Store) -> None:
        # ADR-0005: events carry correlation ids from day one, so the deferred
        # OTel tailer can link spans from recorded data alone.
        make_node(store, "n1", NodeState.READY)
        store.set_state("n1", NodeState.IN_PROGRESS, now=at(1), attempt=3)
        events = [e for e in store.events_after(0) if e.kind == "state_changed"]
        assert events[-1].attempt == 3

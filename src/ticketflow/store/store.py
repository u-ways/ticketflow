"""The canonical SQLite state store (ADR-0003).

The orchestrator process is the only writer. WAL mode, busy_timeout set.
State changes go through the transition table (ADR-0006) and are evented
atomically in the same transaction (ADR-0005). Events are append-only: this
class deliberately exposes no way to mutate or delete them.

All timestamps are injected by the caller (the orchestrator owns the clock),
which keeps every method deterministic and unit-testable.
"""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from ticketflow.domain.errors import UnknownNode
from ticketflow.domain.model import (
    Attempt,
    Event,
    ExternalRef,
    Intent,
    Lease,
    Node,
    NodeState,
)
from ticketflow.domain.transitions import assert_legal
from ticketflow.store.migrations import apply_migrations

_BUSY_TIMEOUT_MS = 5_000


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _from_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class Store:
    """One database file; one writer; projections may lag (ADR-0003)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path | str) -> Self:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        apply_migrations(conn)
        return cls(conn)

    @classmethod
    def open_read_only(cls, path: Path | str) -> Self:
        """Reader connection for status views (ADR-0003): no writes, no DDL."""
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _txn(self) -> Iterator[None]:
        """Explicit transaction. The connection runs in autocommit mode, so
        paired writes (a state row and its event, ADR-0005) need a real
        BEGIN/COMMIT to be one unit of work."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def journal_mode(self) -> str:
        row = self._conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0])

    # -- nodes ------------------------------------------------------------

    def insert_node(
        self,
        *,
        node_id: str,
        title: str,
        body: str,
        state: NodeState,
        now: datetime,
        scope_hints: Sequence[str] = (),
        blocked_reason: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO nodes
                (node_id, title, body, state, blocked_reason, scope_hints,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                title,
                body,
                state.value,
                blocked_reason,
                json.dumps(list(scope_hints)),
                _iso(now),
                _iso(now),
            ),
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        return self._node_from_row(row) if row else None

    def _require_node(self, node_id: str) -> Node:
        node = self.get_node(node_id)
        if node is None:
            raise UnknownNode(f"no such node: {node_id}")
        return node

    def list_nodes(self, state: NodeState | None = None) -> list[Node]:
        if state is None:
            rows = self._conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE state = ? ORDER BY node_id", (state.value,)
            ).fetchall()
        return [self._node_from_row(row) for row in rows]

    def update_node_content(
        self,
        node_id: str,
        *,
        title: str,
        body: str,
        scope_hints: Sequence[str],
        now: datetime,
    ) -> None:
        self._require_node(node_id)
        self._conn.execute(
            """
            UPDATE nodes
            SET title = ?, body = ?, scope_hints = ?, updated_at = ?
            WHERE node_id = ?
            """,
            (title, body, json.dumps(list(scope_hints)), _iso(now), node_id),
        )

    def set_state(
        self,
        node_id: str,
        to_state: NodeState,
        *,
        now: datetime,
        reason: str | None = None,
        attempt: int | None = None,
    ) -> Node:
        """Apply a state transition, atomically with its event (ADR-0005/0006)."""
        node = self._require_node(node_id)
        assert_legal(node.state, to_state)
        with self._txn():
            self._conn.execute(
                """
                UPDATE nodes
                SET state = ?, blocked_reason = ?, updated_at = ?
                WHERE node_id = ?
                """,
                (to_state.value, reason, _iso(now), node_id),
            )
            self._append_event_row(
                kind="state_changed",
                now=now,
                node_id=node_id,
                attempt=attempt,
                payload={
                    "from": node.state.value,
                    "to": to_state.value,
                    "reason": reason,
                },
            )
        return self._require_node(node_id)

    def set_blocked_reason(self, node_id: str, reason: str | None, *, now: datetime) -> None:
        self._require_node(node_id)
        self._conn.execute(
            "UPDATE nodes SET blocked_reason = ?, updated_at = ? WHERE node_id = ?",
            (reason, _iso(now), node_id),
        )

    def bump_attempt_count(self, node_id: str, *, now: datetime) -> int:
        return self._bump(node_id, "attempt_count", now)

    def bump_cycle_count(self, node_id: str, *, now: datetime) -> int:
        return self._bump(node_id, "cycle_count", now)

    def _bump(self, node_id: str, column: str, now: datetime) -> int:
        self._require_node(node_id)
        row = self._conn.execute(
            f"""
            UPDATE nodes SET {column} = {column} + 1, updated_at = ?
            WHERE node_id = ? RETURNING {column}
            """,
            (_iso(now), node_id),
        ).fetchone()
        return int(row[0])

    def reset_counters(self, node_id: str, *, now: datetime) -> None:
        self._require_node(node_id)
        self._conn.execute(
            """
            UPDATE nodes SET attempt_count = 0, cycle_count = 0, updated_at = ?
            WHERE node_id = ?
            """,
            (_iso(now), node_id),
        )

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> Node:
        return Node(
            node_id=row["node_id"],
            title=row["title"],
            body=row["body"],
            state=NodeState(row["state"]),
            blocked_reason=row["blocked_reason"],
            attempt_count=row["attempt_count"],
            cycle_count=row["cycle_count"],
            scope_hints=tuple(json.loads(row["scope_hints"])),
            created_at=_from_iso(row["created_at"]),
            updated_at=_from_iso(row["updated_at"]),
        )

    # -- external refs ----------------------------------------------------

    def link_external(
        self, node_id: str, *, provider: str, external_key: str, etag: str | None = None
    ) -> None:
        self._require_node(node_id)
        self._conn.execute(
            """
            INSERT INTO external_refs (node_id, provider, external_key, etag)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (provider, external_key)
            DO UPDATE SET node_id = excluded.node_id, etag = excluded.etag
            """,
            (node_id, provider, external_key, etag),
        )

    def resolve_external(self, provider: str, external_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT node_id FROM external_refs WHERE provider = ? AND external_key = ?",
            (provider, external_key),
        ).fetchone()
        return str(row["node_id"]) if row else None

    def refs_for(self, node_id: str) -> list[ExternalRef]:
        rows = self._conn.execute(
            "SELECT * FROM external_refs WHERE node_id = ? ORDER BY provider, external_key",
            (node_id,),
        ).fetchall()
        return [
            ExternalRef(
                node_id=row["node_id"],
                provider=row["provider"],
                external_key=row["external_key"],
                etag=row["etag"],
            )
            for row in rows
        ]

    # -- edges ------------------------------------------------------------

    def replace_upstreams(self, node_id: str, upstream_ids: Sequence[str]) -> None:
        with self._txn():
            self._conn.execute("DELETE FROM edges WHERE to_node = ?", (node_id,))
            self._conn.executemany(
                "INSERT OR IGNORE INTO edges (from_node, to_node) VALUES (?, ?)",
                [(up, node_id) for up in upstream_ids],
            )

    def upstreams_of(self, node_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT from_node FROM edges WHERE to_node = ? ORDER BY from_node", (node_id,)
        ).fetchall()
        return tuple(row["from_node"] for row in rows)

    def downstreams_of(self, node_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT to_node FROM edges WHERE from_node = ? ORDER BY to_node", (node_id,)
        ).fetchall()
        return tuple(row["to_node"] for row in rows)

    def all_edges(self) -> set[tuple[str, str]]:
        rows = self._conn.execute("SELECT from_node, to_node FROM edges").fetchall()
        return {(row["from_node"], row["to_node"]) for row in rows}

    # -- leases -----------------------------------------------------------

    def claim_lease(
        self,
        node_id: str,
        *,
        worker_id: str,
        attempt: int,
        ttl_seconds: int,
        now: datetime,
    ) -> bool:
        """Claim exclusively; succeeds only if no unexpired lease exists."""
        self._require_node(node_id)
        expires = _iso(now + timedelta(seconds=ttl_seconds))
        with self._txn():
            self._conn.execute(
                "DELETE FROM leases WHERE node_id = ? AND expires_at <= ?",
                (node_id, _iso(now)),
            )
            try:
                self._conn.execute(
                    "INSERT INTO leases (node_id, worker_id, attempt, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (node_id, worker_id, attempt, expires),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def extend_lease(self, node_id: str, *, ttl_seconds: int, now: datetime) -> None:
        self._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE node_id = ?",
            (_iso(now + timedelta(seconds=ttl_seconds)), node_id),
        )

    def release_lease(self, node_id: str) -> None:
        self._conn.execute("DELETE FROM leases WHERE node_id = ?", (node_id,))

    def get_lease(self, node_id: str) -> Lease | None:
        row = self._conn.execute("SELECT * FROM leases WHERE node_id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        expires_at = _from_iso(row["expires_at"])
        assert expires_at is not None
        return Lease(
            node_id=row["node_id"],
            worker_id=row["worker_id"],
            attempt=row["attempt"],
            expires_at=expires_at,
        )

    def expire_stale_leases(self, *, now: datetime) -> tuple[str, ...]:
        rows = self._conn.execute(
            "DELETE FROM leases WHERE expires_at <= ? RETURNING node_id", (_iso(now),)
        ).fetchall()
        return tuple(sorted(row["node_id"] for row in rows))

    # -- attempts ---------------------------------------------------------

    def create_attempt(
        self,
        node_id: str,
        *,
        attempt: int,
        runner: str,
        run_dir: str,
        now: datetime,
        model: str | None = None,
    ) -> bool:
        """Idempotent: re-creating the same (node, attempt) is a no-op."""
        self._require_node(node_id)
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO attempts
                (node_id, attempt, runner, model, run_dir, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_id, attempt, runner, model, run_dir, _iso(now)),
        )
        return cursor.rowcount == 1

    def update_attempt(
        self,
        node_id: str,
        attempt: int,
        *,
        pid: int | None = None,
        create_time: float | None = None,
        session_id: str | None = None,
        status: str | None = None,
        exit_code: int | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "pid": pid,
            "create_time": create_time,
            "session_id": session_id,
            "status": status,
            "exit_code": exit_code,
            "finished_at": _iso(finished_at) if finished_at else None,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return
        assignments = ", ".join(f"{column} = ?" for column in updates)
        self._conn.execute(
            f"UPDATE attempts SET {assignments} WHERE node_id = ? AND attempt = ?",
            (*updates.values(), node_id, attempt),
        )

    def get_attempt(self, node_id: str, attempt: int) -> Attempt | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE node_id = ? AND attempt = ?",
            (node_id, attempt),
        ).fetchone()
        return self._attempt_from_row(row) if row else None

    def running_attempts(self) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE status = 'running' ORDER BY node_id, attempt"
        ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> Attempt:
        return Attempt(
            node_id=row["node_id"],
            attempt=row["attempt"],
            runner=row["runner"],
            run_dir=row["run_dir"],
            status=row["status"],
            model=row["model"],
            pid=row["pid"],
            create_time=row["create_time"],
            session_id=row["session_id"],
            exit_code=row["exit_code"],
            started_at=_from_iso(row["started_at"]),
            finished_at=_from_iso(row["finished_at"]),
        )

    # -- intents ----------------------------------------------------------

    def add_intent(
        self,
        *,
        intent_type: str,
        source: str,
        now: datetime,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
        external_id: str | None = None,
    ) -> int | None:
        """Append a human signal. Returns None if external_id already seen."""
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO intents
                (intent_type, source, node_id, payload, external_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                intent_type,
                source,
                node_id,
                json.dumps(payload or {}),
                external_id,
                _iso(now),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid) if cursor.lastrowid else None

    def unprocessed_intents(self) -> list[Intent]:
        rows = self._conn.execute(
            "SELECT * FROM intents WHERE processed_at IS NULL ORDER BY intent_id"
        ).fetchall()
        return [
            Intent(
                intent_id=row["intent_id"],
                intent_type=row["intent_type"],
                source=row["source"],
                node_id=row["node_id"],
                payload=json.loads(row["payload"]),
                created_at=_from_iso(row["created_at"]),
                processed_at=_from_iso(row["processed_at"]),
            )
            for row in rows
        ]

    def mark_intent_processed(self, intent_id: int, *, now: datetime) -> None:
        self._conn.execute(
            "UPDATE intents SET processed_at = ? WHERE intent_id = ? AND processed_at IS NULL",
            (_iso(now), intent_id),
        )

    # -- events (append-only; no update or delete exists, ADR-0005) -------

    def append_event(
        self,
        kind: str,
        *,
        now: datetime,
        node_id: str | None = None,
        attempt: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self._txn():
            return self._append_event_row(
                kind=kind, now=now, node_id=node_id, attempt=attempt, payload=payload or {}
            )

    def _append_event_row(
        self,
        *,
        kind: str,
        now: datetime,
        node_id: str | None,
        attempt: int | None,
        payload: dict[str, Any],
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO events (ts, kind, node_id, attempt, payload) VALUES (?, ?, ?, ?, ?)",
            (_iso(now), kind, node_id, attempt, json.dumps(payload)),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def events_after(self, cursor: int, *, limit: int = 1000) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?",
            (cursor, limit),
        ).fetchall()
        events: list[Event] = []
        for row in rows:
            ts = _from_iso(row["ts"])
            assert ts is not None
            events.append(
                Event(
                    event_id=row["event_id"],
                    ts=ts,
                    kind=row["kind"],
                    node_id=row["node_id"],
                    attempt=row["attempt"],
                    payload=json.loads(row["payload"]),
                )
            )
        return events

    # -- handoffs (ADR-0013) ----------------------------------------------

    def set_handoff(self, node_id: str, content: str, *, now: datetime) -> None:
        self._require_node(node_id)
        self._conn.execute(
            """
            INSERT INTO handoffs (node_id, content, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (node_id)
            DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
            """,
            (node_id, content, _iso(now)),
        )

    def get_handoff(self, node_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT content FROM handoffs WHERE node_id = ?", (node_id,)
        ).fetchone()
        return str(row["content"]) if row else None

    # -- per-check flake tracking (ADR-0009) ------------------------------

    def record_check_outcome(
        self,
        check_name: str,
        *,
        flaked: bool,
        now: datetime,
        node_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        """Record one check observation: flake counter and event row are one
        unit of work (ADR-0005, ADR-0009)."""
        with self._txn():
            self._conn.execute(
                """
                INSERT INTO check_stats (check_name, runs, flakes) VALUES (?, 1, ?)
                ON CONFLICT (check_name)
                DO UPDATE SET runs = runs + 1, flakes = flakes + excluded.flakes
                """,
                (check_name, 1 if flaked else 0),
            )
            self._append_event_row(
                kind="check_observed",
                now=now,
                node_id=node_id,
                attempt=attempt,
                payload={"check": check_name, "flaked": flaked},
            )

    def flake_rate(self, check_name: str) -> float:
        row = self._conn.execute(
            "SELECT runs, flakes FROM check_stats WHERE check_name = ?", (check_name,)
        ).fetchone()
        if row is None or row["runs"] == 0:
            return 0.0
        return float(row["flakes"]) / float(row["runs"])

    # -- kv bookkeeping (migration 2) --------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def kv_delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))

"""Plain sequential schema migrations, applied via the user_version pragma.

No ORM, no migration framework (ADR-0003). Migration N moves user_version
from N-1 to N; migrations are append-only and never edited once released.
"""

import sqlite3

MIGRATIONS: tuple[str, ...] = (
    # 1: initial schema.
    """
    CREATE TABLE nodes (
        node_id        TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        body           TEXT NOT NULL DEFAULT '',
        state          TEXT NOT NULL,
        blocked_reason TEXT,
        attempt_count  INTEGER NOT NULL DEFAULT 0,
        cycle_count    INTEGER NOT NULL DEFAULT 0,
        scope_hints    TEXT NOT NULL DEFAULT '[]',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );

    CREATE TABLE external_refs (
        node_id      TEXT NOT NULL REFERENCES nodes(node_id),
        provider     TEXT NOT NULL,
        external_key TEXT NOT NULL,
        etag         TEXT,
        PRIMARY KEY (provider, external_key)
    );

    CREATE TABLE edges (
        from_node TEXT NOT NULL,
        to_node   TEXT NOT NULL,
        PRIMARY KEY (from_node, to_node)
    );

    CREATE TABLE attempts (
        node_id     TEXT NOT NULL,
        attempt     INTEGER NOT NULL,
        runner      TEXT NOT NULL,
        model       TEXT,
        status      TEXT NOT NULL DEFAULT 'running',
        run_dir     TEXT NOT NULL,
        pid         INTEGER,
        create_time REAL,
        session_id  TEXT,
        exit_code   INTEGER,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        PRIMARY KEY (node_id, attempt)
    );

    CREATE TABLE leases (
        node_id    TEXT PRIMARY KEY,
        worker_id  TEXT NOT NULL,
        attempt    INTEGER NOT NULL,
        expires_at TEXT NOT NULL
    );

    CREATE TABLE intents (
        intent_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        intent_type  TEXT NOT NULL,
        source       TEXT NOT NULL,
        node_id      TEXT,
        payload      TEXT NOT NULL DEFAULT '{}',
        external_id  TEXT UNIQUE,
        created_at   TEXT NOT NULL,
        processed_at TEXT
    );

    CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        kind     TEXT NOT NULL,
        node_id  TEXT,
        attempt  INTEGER,
        payload  TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE handoffs (
        node_id    TEXT PRIMARY KEY REFERENCES nodes(node_id),
        content    TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE check_stats (
        check_name TEXT PRIMARY KEY,
        runs       INTEGER NOT NULL DEFAULT 0,
        flakes     INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX idx_nodes_state ON nodes(state);
    CREATE INDEX idx_events_node ON events(node_id);
    CREATE INDEX idx_intents_pending ON intents(processed_at) WHERE processed_at IS NULL;
    """,
    # 2: orchestrator bookkeeping — sync/projection cursors, dispatch pause,
    # pending feedback, per-node flags. Values are opaque strings.
    """
    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring the database schema up to the latest version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version > current:
            with conn:
                conn.executescript(script)
                conn.execute(f"PRAGMA user_version = {version}")

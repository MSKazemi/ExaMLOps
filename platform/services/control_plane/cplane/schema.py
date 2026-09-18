"""Control-plane tables, indices and additive column migrations (SQLite and Postgres)."""

from __future__ import annotations

from typing import Any

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    id            TEXT PRIMARY KEY,
    model_id      TEXT NOT NULL,
    commit_sha    TEXT,
    commit_msg    TEXT,
    changed_files TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    prefect_run_id TEXT,
    reject_reason  TEXT,
    tenant         TEXT NOT NULL DEFAULT 'default',
    requested_by   TEXT NOT NULL DEFAULT 'legacy',
    resolved_by    TEXT,
    requested_at  TEXT NOT NULL,
    resolved_at   TEXT
)
"""

_CREATE_MODELZOO_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS modelzoo_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha TEXT NOT NULL,
    branch     TEXT NOT NULL,
    pushed_by  TEXT,
    timestamp  TEXT NOT NULL,
    source     TEXT NOT NULL,
    raw_payload TEXT
)
"""

_CREATE_MODEL_FRESHNESS_SQL = """
CREATE TABLE IF NOT EXISTS model_freshness (
    model_id               TEXT PRIMARY KEY,
    latest_modelzoo_commit TEXT,
    last_retrain_commit    TEXT,
    is_stale               INTEGER NOT NULL DEFAULT 0,
    stale_since            TEXT,
    retrain_triggered_at   TEXT
)
"""

_CREATE_COMMANDS_SQL = """
CREATE TABLE IF NOT EXISTS control_plane_commands (
    command_key    TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    payload        TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    response       TEXT,
    prefect_run_id TEXT,
    approval_id    TEXT,
    admission_id   INTEGER,
    actor          TEXT NOT NULL DEFAULT 'system',
    tenant         TEXT NOT NULL DEFAULT 'default',
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""

# These are the same durable queue and transactional-outbox contracts exposed by
# ``examlops.admission`` and ``examlops.events``. They live in the control-plane state database so
# the command, queue transition, approval transition, and emitted event can share one transaction
# on both SQLite and Postgres. ``enqueue_event(..., conn=...)`` below uses the shared outbox helper.
_CREATE_ADMISSION_SQL = """
CREATE TABLE IF NOT EXISTS admission_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant      TEXT NOT NULL DEFAULT 'default',
    project     TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'queued',
    enqueued_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at  DATETIME,
    finished_at DATETIME,
    reason      TEXT
)
"""

# Runtime settings an operator changes through the API (the ModelZoo poll interval and auto-retrain
# switch today). In the shared state store, not in process memory, so every replica acts on the
# same value: a change made through one replica used to reach only that replica.
_CREATE_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS control_plane_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT 'system'
)
"""

_CREATE_OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS event_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at DATETIME,
    claimed_at   DATETIME,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    actor        TEXT NOT NULL DEFAULT 'system',
    tenant       TEXT NOT NULL DEFAULT 'default',
    traceparent  TEXT
)
"""

_SCHEMA_COLUMNS = {
    "pending_approvals": {
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        "requested_by": "TEXT NOT NULL DEFAULT 'legacy'",
        "resolved_by": "TEXT",
    },
    "control_plane_commands": {
        "actor": "TEXT NOT NULL DEFAULT 'system'",
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        # `sync` = claimed and dispatched inside the HTTP request (legacy routes); `async` = queued
        # by /v1 and dispatched by the worker pool. Only async commands are ever retried in the
        # background: a synchronous caller was already told its request failed (plan P1.2).
        "mode": "TEXT NOT NULL DEFAULT 'sync'",
        # The Prefect flow run's own state, followed by the reconciler (plan P1.2b). `succeeded`
        # on the command means "a run was created"; this says whether the training finished.
        "run_state": "TEXT",
        "run_state_at": "TEXT",
    },
    "event_outbox": {
        "actor": "TEXT NOT NULL DEFAULT 'system'",
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        "traceparent": "TEXT",
    },
}

_CREATE_INDICES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_pa_model_status ON pending_approvals(model_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pa_tenant_model_status "
    "ON pending_approvals(tenant, model_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_me_sha ON modelzoo_events(commit_sha)",
    "CREATE INDEX IF NOT EXISTS idx_cp_commands_state ON control_plane_commands(state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_admission_state_tenant ON admission_queue(state, tenant, priority, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_outbox_unpublished ON event_outbox(published_at, id)",
]


def _apply_schema_migrations(conn: Any) -> None:
    """Add identity columns without replacing existing SQLite or PostgreSQL tables."""
    for table, definitions in _SCHEMA_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in definitions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

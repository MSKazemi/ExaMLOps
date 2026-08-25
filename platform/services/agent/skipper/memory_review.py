"""SM3 — review-queue for procedure writes (ADR 0034, BL-009).

Instead of an inline HITL interrupt on every ``record_procedure``, queue pending procedures
for **batch** operator review (list → approve/reject), extending the Phase-11 approval-gate
pattern to agent memory writes. Approving commits the procedure to the memory store; rejecting
drops it. Off by default (``AGENT_MEMORY_REVIEW_QUEUE``) — inline HITL stays the default.

The queue is a small SQLite table in its own file (``AGENT_MEMORY_REVIEW_DB``), separate from
the LangGraph memory store — only *approved* procedures ever reach the store. Pure sqlite;
``approve`` takes the store as an argument so the module never imports langgraph itself.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from skipper import config

_DDL = """
CREATE TABLE IF NOT EXISTS procedure_review_queue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_class         TEXT NOT NULL,
    steps_json         TEXT NOT NULL,
    success_conditions TEXT,
    operator           TEXT,
    principal          TEXT,
    tenant             TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    reason             TEXT,
    reviewer           TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at        TEXT
);
"""


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.AGENT_MEMORY_REVIEW_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(procedure_review_queue)")}
    if "principal" not in columns:
        conn.execute("ALTER TABLE procedure_review_queue ADD COLUMN principal TEXT")
    if "tenant" not in columns:
        conn.execute("ALTER TABLE procedure_review_queue ADD COLUMN tenant TEXT")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["steps"] = json.loads(d.pop("steps_json", None) or "[]")
    return d


def enqueue(
    task_class: str,
    steps: list[str],
    success_conditions: str = "",
    *,
    operator: str | None = None,
    db_path: str | None = None,
) -> int:
    """Queue a procedure for review; returns the review id."""
    from skipper import scoping

    identity = scoping.request_identity()
    actor = operator or (identity.principal if identity else config.AGENT_ACTOR)
    principal = identity.principal if identity else actor
    tenant = identity.tenant if identity else config.AGENT_TENANT
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO procedure_review_queue "
            "(task_class, steps_json, success_conditions, operator, principal, tenant) "
            "VALUES (?,?,?,?,?,?)",
            (task_class, json.dumps(steps), success_conditions, actor, principal, tenant),
        )
        return int(cur.lastrowid or 0)


def list_pending(
    db_path: str | None = None,
    *,
    principal: str | None = None,
    tenant: str | None = None,
) -> list[dict[str, Any]]:
    """All pending reviews, oldest first."""
    where = "status='pending'"
    params: tuple[Any, ...] = ()
    if principal is not None and tenant is not None:
        where += " AND principal=? AND tenant=?"
        params = (principal, tenant)
    with _conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM procedure_review_queue WHERE {where} ORDER BY id",  # noqa: S608
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get(
    review_id: int,
    db_path: str | None = None,
    *,
    principal: str | None = None,
    tenant: str | None = None,
) -> dict[str, Any] | None:
    where = "id=?"
    params: tuple[Any, ...] = (review_id,)
    if principal is not None and tenant is not None:
        where += " AND principal=? AND tenant=?"
        params = (review_id, principal, tenant)
    with _conn(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM procedure_review_queue WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
    return _row_to_dict(row) if row else None


def approve(
    review_id: int,
    store: Any,
    *,
    reviewer: str | None = None,
    db_path: str | None = None,
    principal: str | None = None,
    tenant: str | None = None,
) -> str:
    """Commit a pending procedure to the memory ``store`` and mark it approved."""
    from skipper import memory_types

    # Read the pending row and release the queue lock BEFORE writing to the store — never hold a
    # queue-db transaction open across the external store write (which runs its own transaction).
    where = "id=? AND status='pending'"
    params: tuple[Any, ...] = (review_id,)
    if principal is not None and tenant is not None:
        where += " AND principal=? AND tenant=?"
        params = (review_id, principal, tenant)
    conn = _conn(db_path)
    try:
        row = conn.execute(
            f"SELECT * FROM procedure_review_queue WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return f"No pending review #{review_id}."

    from skipper import scoping

    if principal is None or tenant is None:
        memory_types.record_procedure(
            store,
            row["task_class"],
            json.loads(row["steps_json"]),
            row["success_conditions"] or "",
            operator=row["operator"] or config.AGENT_ACTOR,
        )
    else:
        with scoping.identity_scope(principal, tenant):
            memory_types.record_procedure(
                store,
                row["task_class"],
                json.loads(row["steps_json"]),
                row["success_conditions"] or "",
                operator=principal,
            )

    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE procedure_review_queue SET status='approved', reviewer=?, "
            "reviewed_at=datetime('now') WHERE id=?",
            (reviewer or config.AGENT_ACTOR, review_id),
        )
    memory_types.audit_memory_op(
        "memory_review_approve",
        "proc",
        row["task_class"],
        reviewer or config.AGENT_ACTOR,
        f"review={review_id}",
        None,
    )
    return (
        f"Approved review #{review_id} — procedure for '{row['task_class']}' committed to memory."
    )


def reject(
    review_id: int,
    *,
    reviewer: str | None = None,
    reason: str = "",
    db_path: str | None = None,
    principal: str | None = None,
    tenant: str | None = None,
) -> str:
    """Mark a pending review rejected (the procedure is never written to the store)."""
    where = "id=? AND status='pending'"
    params: tuple[Any, ...] = (reviewer or config.AGENT_ACTOR, reason, review_id)
    if principal is not None and tenant is not None:
        where += " AND principal=? AND tenant=?"
        params = (*params, principal, tenant)
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE procedure_review_queue SET status='rejected', reviewer=?, reason=?, "
            f"reviewed_at=datetime('now') WHERE {where}",  # noqa: S608
            params,
        )
        n = cur.rowcount
    if n:
        from skipper import memory_types

        memory_types.audit_memory_op(
            "memory_review_reject",
            "proc",
            None,
            reviewer or config.AGENT_ACTOR,
            f"review={review_id}",
            None,
        )
    return f"Rejected review #{review_id}." if n else f"No pending review #{review_id}."

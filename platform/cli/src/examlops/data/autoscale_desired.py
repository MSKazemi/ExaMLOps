"""examlops.data.autoscale_desired — the autoscaler's *desired replicas* per model (ADR 0031).

The controller's ``desired`` applier writes here. Nothing in the platform reads this table to
change a replica count by itself: it is the hand-off point for whatever actually owns replicas
(an operator, a deploy step, or a KEDA/Knative generator). It is a statement of intent, not proof
that a replica exists. The table is created lazily (``CREATE TABLE IF NOT EXISTS``) so no
``platform_db`` schema change is needed.
"""

from __future__ import annotations

from typing import Any

from examlops.platform_db import get_db, init_db, write_retry

__all__ = ["get_desired", "list_desired", "set_desired"]

_DDL = """CREATE TABLE IF NOT EXISTS autoscale_desired (
    model      TEXT PRIMARY KEY,
    replicas   INTEGER NOT NULL CHECK (replicas >= 0),
    reason     TEXT,
    updated_by TEXT,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


def set_desired(
    model: str, replicas: int, *, reason: str = "", updated_by: str | None = None
) -> None:
    if not (model or "").strip():
        raise ValueError("a model name is required")
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
        raise ValueError(f"replicas must be a non-negative integer, got {replicas!r}")

    def _do() -> None:
        init_db()
        with get_db() as conn:
            conn.execute(_DDL)
            conn.execute(
                "INSERT OR REPLACE INTO autoscale_desired (model, replicas, reason, updated_by, "
                "updated_at) VALUES (?,?,?,?, CURRENT_TIMESTAMP)",
                (model, replicas, reason, updated_by),
            )

    write_retry(_do)


def get_desired(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        conn.execute(_DDL)
        row = conn.execute("SELECT * FROM autoscale_desired WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def list_desired() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        conn.execute(_DDL)
        rows = conn.execute("SELECT * FROM autoscale_desired ORDER BY model").fetchall()
    return [dict(r) for r in rows]

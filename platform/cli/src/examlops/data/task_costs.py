"""examlops.data.task_costs — the per-task cost ledger's storage (ADR 0148 decision 4).

Policy (components, rates, standby apportioning) lives in :mod:`examlops.finops.task_ledger`.
The table is created lazily, so no ``platform_db`` schema change is needed. Each entry has a
caller-supplied ``entry_id`` that is ``UNIQUE`` *per tenant*: re-recording the same metering event
is a no-op, so a retried writer never double counts, and one tenant's id can never swallow another
tenant's write. Every read filters by tenant in SQL, before any LIMIT; totals are SQL aggregates
over every entry, never a sum over a bounded page of rows.
"""

from __future__ import annotations

from typing import Any

from examlops.platform_db import get_db, init_db, write_retry

__all__ = [
    "MAX_ROWS",
    "insert_entries",
    "task_entries",
    "task_totals",
    "window_totals",
]

#: The most rows :func:`task_entries` returns for display. Totals never depend on it.
MAX_ROWS = 10_000

_DDL = """CREATE TABLE IF NOT EXISTS task_cost_entries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    entry_id  TEXT NOT NULL,
    task_id   TEXT NOT NULL,
    project   TEXT NOT NULL,
    tenant    TEXT NOT NULL DEFAULT 'default',
    component TEXT NOT NULL,
    quantity  REAL NOT NULL CHECK (quantity >= 0),
    unit      TEXT NOT NULL,
    cost_usd  REAL NOT NULL CHECK (cost_usd >= 0),
    method    TEXT,
    UNIQUE (tenant, entry_id)
)"""
_IDX = "CREATE INDEX IF NOT EXISTS ix_task_cost_entries_task ON task_cost_entries (tenant, task_id)"
_COLS = "entry_id, task_id, project, tenant, component, quantity, unit, cost_usd, method"


def _ensure(conn: Any) -> None:
    conn.execute(_DDL)
    conn.execute(_IDX)


def _insert_new(conn: Any, rows: list[dict[str, Any]]) -> int:
    created = 0
    for r in rows:
        hit = conn.execute(
            "SELECT 1 FROM task_cost_entries WHERE tenant=? AND entry_id=?",
            (r["tenant"], r["entry_id"]),
        ).fetchone()
        if hit:
            continue
        conn.execute(
            f"INSERT INTO task_cost_entries ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(r[c.strip()] for c in _COLS.split(",")),
        )
        created += 1
    return created


def insert_entries(
    rows: list[dict[str, Any]],
    *,
    exclusive_prefix: str | None = None,
    tenant: str | None = None,
) -> int:
    """Insert entries atomically; returns how many were new (duplicates by ``entry_id`` skip).

    With ``exclusive_prefix`` the batch is all-or-nothing against what is already stored under
    that ``entry_id`` prefix (in ``tenant``): if nothing is stored, every row is written; if
    exactly these rows (ids *and* costs) are stored, nothing is written; anything else raises
    :class:`ValueError` — a second, different split of the same pool period is never half-applied.
    The check and the write share one transaction.
    """

    def _do() -> int:
        init_db()
        with get_db() as conn:
            _ensure(conn)
            if exclusive_prefix is not None:
                stored = _prefix_rows(conn, exclusive_prefix, tenant or "default")
                if stored:
                    wanted = {r["entry_id"]: round(float(r["cost_usd"]), 9) for r in rows}
                    if stored != wanted:
                        raise ValueError(
                            f"{exclusive_prefix!r} already recorded with a different split"
                        )
                    return 0
            return _insert_new(conn, rows)

    return write_retry(_do)


def _prefix_rows(conn: Any, prefix: str, tenant: str) -> dict[str, float]:
    # substr() rather than LIKE: an entry id may legitimately contain '%' or '_'.
    rows = conn.execute(
        "SELECT entry_id, cost_usd FROM task_cost_entries "
        "WHERE tenant=? AND substr(entry_id, 1, ?) = ?",
        (tenant, len(prefix), prefix),
    ).fetchall()
    return {r[0]: round(float(r[1]), 9) for r in rows}


def task_entries(task_id: str, *, tenant: str = "default", limit: int | None = None) -> list[dict]:
    """The task's entries in write order, at most ``limit`` (capped at :data:`MAX_ROWS`)."""
    cap = MAX_ROWS if limit is None else max(1, min(int(limit), MAX_ROWS))
    init_db()
    with get_db() as conn:
        _ensure(conn)
        rows = conn.execute(
            f"SELECT id, ts, {_COLS} FROM task_cost_entries WHERE tenant=? AND task_id=? "
            "ORDER BY id LIMIT ?",
            (tenant, task_id, cap),
        ).fetchall()
    return [dict(r) for r in rows]


def task_totals(task_id: str, *, tenant: str = "default") -> dict[str, Any]:
    """Unbounded per-component totals for one task (SQL aggregates over *every* entry)."""
    init_db()
    with get_db() as conn:
        _ensure(conn)
        comp = conn.execute(
            "SELECT component, SUM(cost_usd), COUNT(*) FROM task_cost_entries "
            "WHERE tenant=? AND task_id=? GROUP BY component",
            (tenant, task_id),
        ).fetchall()
        projects = conn.execute(
            "SELECT DISTINCT project FROM task_cost_entries WHERE tenant=? AND task_id=?",
            (tenant, task_id),
        ).fetchall()
    return {
        "components": {r[0]: float(r[1] or 0.0) for r in comp},
        "entries": sum(int(r[2]) for r in comp),
        "projects": sorted(r[0] for r in projects),
    }


def window_totals(since: str | None = None, *, tenant: str | None = None) -> dict[str, Any]:
    """Per-component totals and the number of distinct tasks over an optional window."""
    where, args = ["1=1"], []
    if since:
        where.append("ts >= ?")
        args.append(since)
    if tenant:
        where.append("tenant = ?")
        args.append(tenant)
    init_db()
    with get_db() as conn:
        _ensure(conn)
        comp = conn.execute(
            f"SELECT component, SUM(cost_usd), COUNT(*) FROM task_cost_entries "
            f"WHERE {' AND '.join(where)} GROUP BY component",
            args,
        ).fetchall()
        tasks = conn.execute(
            f"SELECT COUNT(DISTINCT task_id) FROM task_cost_entries WHERE {' AND '.join(where)}",
            args,
        ).fetchone()[0]
    return {
        "tasks": int(tasks or 0),
        "components": {r[0]: {"cost_usd": float(r[1] or 0.0), "entries": int(r[2])} for r in comp},
    }

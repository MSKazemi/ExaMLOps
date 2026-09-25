"""examlops.data.generative_benchmarks — benchmark results stored *with* their conditions.

Policy (which conditions are required, the percentile maths) lives in
:mod:`examlops.slo.benchmarks`; this module only touches ``generative_benchmarks``. The table is
created lazily (``CREATE TABLE IF NOT EXISTS``), so no ``platform_db`` schema change is needed.
The ``conditions`` column is ``NOT NULL`` and a CHECK refuses an empty object, so a number without
its conditions cannot be stored even by a caller that bypasses the policy module.
"""

from __future__ import annotations

import json
from typing import Any

from examlops.platform_db import get_db, init_db, write_retry

__all__ = ["get", "insert", "list_results"]

_DDL = """CREATE TABLE IF NOT EXISTS generative_benchmarks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    servable           TEXT NOT NULL,
    tenant             TEXT NOT NULL DEFAULT 'default',
    conditions         TEXT NOT NULL CHECK (length(conditions) > 2),
    digest             TEXT NOT NULL,
    n                  INTEGER NOT NULL,
    rejected           INTEGER NOT NULL DEFAULT 0,
    ttft_p50_ms        REAL,
    ttft_p99_ms        REAL,
    tpot_p50_ms        REAL,
    tpot_p99_ms        REAL,
    goodput            REAL,
    verdict            TEXT,
    ttft_includes_queue INTEGER NOT NULL DEFAULT 0,
    recorded_by        TEXT,
    UNIQUE (servable, tenant, digest)
)"""
_IDX = (
    "CREATE INDEX IF NOT EXISTS ix_generative_benchmarks_servable "
    "ON generative_benchmarks (tenant, servable, id DESC)"
)


def _ensure(conn: Any) -> None:
    conn.execute(_DDL)
    conn.execute(_IDX)


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["conditions"] = json.loads(d["conditions"])
    d["ttft_includes_queue"] = bool(d["ttft_includes_queue"])
    return d


def insert(row: dict[str, Any]) -> tuple[int, bool]:
    """Insert one result; ``(id, created)``. Idempotent on ``(servable, tenant, digest)``."""

    def _do() -> tuple[int, bool]:
        init_db()
        with get_db() as conn:
            _ensure(conn)
            hit = conn.execute(
                "SELECT id FROM generative_benchmarks WHERE servable=? AND tenant=? AND digest=?",
                (row["servable"], row["tenant"], row["digest"]),
            ).fetchone()
            if hit:
                return int(hit[0]), False
            cur = conn.execute(
                "INSERT INTO generative_benchmarks (servable, tenant, conditions, digest, n, "
                "rejected, ttft_p50_ms, ttft_p99_ms, tpot_p50_ms, tpot_p99_ms, goodput, verdict, "
                "ttft_includes_queue, recorded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["servable"],
                    row["tenant"],
                    json.dumps(row["conditions"], sort_keys=True),
                    row["digest"],
                    row["n"],
                    row["rejected"],
                    row.get("ttft_p50_ms"),
                    row.get("ttft_p99_ms"),
                    row.get("tpot_p50_ms"),
                    row.get("tpot_p99_ms"),
                    row.get("goodput"),
                    row.get("verdict"),
                    int(bool(row.get("ttft_includes_queue"))),
                    row.get("recorded_by"),
                ),
            )
            return int(cur.lastrowid or 0), True

    return write_retry(_do)


def get(result_id: int, tenant: str = "default") -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        _ensure(conn)
        r = conn.execute(
            "SELECT * FROM generative_benchmarks WHERE id=? AND tenant=?", (result_id, tenant)
        ).fetchone()
    return _row(r) if r else None


def list_results(
    servable: str | None = None, *, tenant: str = "default", limit: int = 50
) -> list[dict[str, Any]]:
    """Newest first. The tenant (and servable) filter is in the WHERE, before the LIMIT."""
    limit = max(1, min(int(limit), 500))
    where, args = ["tenant=?"], [tenant]
    if servable:
        where.append("servable=?")
        args.append(servable)
    init_db()
    with get_db() as conn:
        _ensure(conn)
        rows = conn.execute(
            f"SELECT * FROM generative_benchmarks WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    return [_row(r) for r in rows]

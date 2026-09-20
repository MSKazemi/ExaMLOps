"""examlops.data.slo_pairs — storage for paired (TTFT, TPOT) serving SLOs (ADR 0117 decision 2).

Policy (validation, evaluation) lives in :mod:`examlops.slo.pairs`; this module only touches
``slo_pairs``.
"""

from __future__ import annotations

import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = ["delete", "get", "init_db", "list_pairs", "put"]

_COLS = "model, name, tenant, ttft_ms, tpot_ms, percentile, tight, slo_class, updated_at"


def put(
    model: str,
    name: str,
    tenant: str,
    ttft_ms: float,
    tpot_ms: float,
    percentile: float,
    tight: str,
    slo_class: str,
) -> str:
    """Insert or replace one pair. Returns ``"created"`` or ``"updated"``."""

    def _do() -> str:
        init_db()  # schema-once sentinel: cheap after the first call
        now = time.time()
        with _immediate_write("slo_pairs") as conn:
            cur = conn.execute(
                "UPDATE slo_pairs SET ttft_ms=?, tpot_ms=?, percentile=?, tight=?, slo_class=?, "
                "updated_at=? WHERE model=? AND name=? AND tenant=?",
                (ttft_ms, tpot_ms, percentile, tight, slo_class, now, model, name, tenant),
            )
            if cur.rowcount == 1:
                return "updated"
            conn.execute(
                f"INSERT INTO slo_pairs ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model, name, tenant, ttft_ms, tpot_ms, percentile, tight, slo_class, now),
            )
            return "created"

    return write_retry(_do)


def get(model: str, name: str, tenant: str = "default") -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()  # schema-once sentinel: cheap after the first call
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM slo_pairs WHERE model=? AND name=? AND tenant=?",
                (model, name, tenant),
            ).fetchone()
            return dict(row) if row else None

    return write_retry(_do)


def list_pairs(model: str | None = None, tenant: str | None = None) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()  # schema-once sentinel: cheap after the first call
        where, args = [], []
        if model:
            where.append("model=?")
            args.append(model)
        if tenant:
            where.append("tenant=?")
            args.append(tenant)
        sql = f"SELECT {_COLS} FROM slo_pairs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with get_db() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY model, name, tenant", args)]

    return write_retry(_do)


def delete(model: str, name: str, tenant: str = "default") -> bool:
    def _do() -> bool:
        init_db()  # schema-once sentinel: cheap after the first call
        with _immediate_write("slo_pairs") as conn:
            cur = conn.execute(
                "DELETE FROM slo_pairs WHERE model=? AND name=? AND tenant=?",
                (model, name, tenant),
            )
            return cur.rowcount == 1

    return write_retry(_do)

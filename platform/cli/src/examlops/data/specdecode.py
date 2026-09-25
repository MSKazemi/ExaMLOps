"""examlops.data.specdecode — speculative-decoding FinOps windows (ADR 0016 decision 4).

One row per **flushed window** of generations for a ``(model, tenant, engine, lookahead)`` key,
never one per request: the accumulator in :mod:`examlops.engines.specdecode` folds calls in memory
and flushes on a call count or an age bound, so a busy gateway adds a handful of rows an hour
rather than one per token stream.

Rows store **token counts, not ratios**. Acceptance rate and speedup are derived at read time from
the summed counts — averaging per-window ratios would weight a 3-token window the same as a
30 000-token one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry

__all__ = [
    "record_specdecode_window",
    "specdecode_summary",
]

#: Hard cap on summary rows returned, whatever the caller asks for.
MAX_SUMMARY_ROWS = 500


def record_specdecode_window(
    model: str,
    *,
    engine: str,
    calls: int,
    proposed_tokens: int,
    accepted_tokens: int,
    lookahead: int = 1,
    tenant: str = "default",
    window_start: str | None = None,
    window_end: str | None = None,
) -> None:
    """Append one flushed window. Counts are clamped at zero (a negative count is a bug upstream,
    not a credit)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO specdecode_windows
                   (model, tenant, engine, window_start, window_end, calls,
                    proposed_tokens, accepted_tokens, lookahead)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                model,
                tenant or "default",
                engine,
                window_start,
                window_end,
                max(int(calls), 0),
                max(int(proposed_tokens), 0),
                max(int(accepted_tokens), 0),
                max(int(lookahead), 1),
            ),
        )


def specdecode_summary(
    *,
    model: str | None = None,
    tenant: str | None = None,
    days: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Summed counts per ``(model, tenant, engine, lookahead)``, newest activity first.

    Every filter — including ``tenant`` — is applied in SQL **before** the ``LIMIT``, so a
    tenant-scoped caller can never have its rows crowded out of the page by another tenant's.
    """
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if model:
        clauses.append("model = ?")
        params.append(model)
    if tenant:
        clauses.append("tenant = ?")
        params.append(tenant)
    if days is not None:
        if days <= 0:
            raise ValueError("days must be > 0")
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        clauses.append("ts >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    cap = max(1, min(int(limit), MAX_SUMMARY_ROWS))
    sql = (
        "SELECT model, tenant, engine, lookahead, COUNT(*) AS windows, SUM(calls) AS calls, "
        "SUM(proposed_tokens) AS proposed_tokens, SUM(accepted_tokens) AS accepted_tokens, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM specdecode_windows"
        f"{where} GROUP BY model, tenant, engine, lookahead "
        "ORDER BY MAX(ts) DESC, model, tenant, engine, lookahead LIMIT ?"
    )
    params.append(cap)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


install_write_retry(__name__)

"""examlops.data.admission — Admission control — fair-share work queue (item 1.5).

Owns these helpers (bodies live here, not in ``platform_db``) — per-domain split (item 4.5) with the
implementation physically relocated. ``platform_db`` re-exports them for back-compat.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from examlops.platform_db import _immediate_write, get_db, write_retry

__all__ = [
    "enqueue_admission",
    "claim_next_admission",
    "complete_admission",
    "admission_stats",
]


def enqueue_admission(
    kind: str,
    payload: dict[str, Any],
    *,
    tenant: str = "default",
    project: str | None = None,
    priority: int = 0,
) -> int:
    """Enqueue a work item into the admission-control queue (Phase 1 item 1.5). Returns its id."""
    payload_json = json.dumps(payload, default=str)

    def _insert() -> int:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO admission_queue (tenant, project, kind, payload, priority) "
                "VALUES (?,?,?,?,?)",
                (tenant, project, kind, payload_json, priority),
            )
            return int(cur.lastrowid or 0)

    return write_retry(_insert)


def claim_next_admission(
    *, max_running: int = 4, per_tenant_cap: int = 2, reclaim_after_s: int = 3600
) -> dict[str, Any] | None:
    """Fair-share dequeue of the next admissible item (Phase 1 item 1.5).

    Enforces a **global concurrency cap** (``max_running`` items ``running`` fleet-wide) and a
    **per-tenant cap**, then picks by **max-min fairness**: among tenants that have a queued item
    and are under their cap, choose the tenant with the fewest currently-running items (ties →
    higher priority, then oldest). Stale ``running`` rows (crashed worker, older than
    ``reclaim_after_s``) are recycled to ``queued`` first. The whole select-and-claim runs under a
    RESERVED write lock, so two schedulers never hand out the same slot. Returns the claimed item
    (now ``running``) or ``None`` if nothing is admissible right now.
    """

    def _claim() -> dict[str, Any] | None:
        with _immediate_write("admission") as conn:
            # Recycle crashed 'running' items whose lease expired.
            conn.execute(
                "UPDATE admission_queue SET state='queued', started_at=NULL "
                "WHERE state='running' "
                "  AND started_at <= datetime(CURRENT_TIMESTAMP, ?)",
                (f"-{max(0, int(reclaim_after_s))} seconds",),
            )
            running_total = conn.execute(
                "SELECT COUNT(*) AS c FROM admission_queue WHERE state='running'"
            ).fetchone()["c"]
            if running_total >= max_running:
                return None
            running_by_tenant = {
                r["tenant"]: r["c"]
                for r in conn.execute(
                    "SELECT tenant, COUNT(*) AS c FROM admission_queue "
                    "WHERE state='running' GROUP BY tenant"
                ).fetchall()
            }
            # Candidate: the best queued item per tenant (priority desc, oldest first).
            candidates = conn.execute(
                "SELECT id, tenant, priority, kind, payload, project FROM admission_queue "
                "WHERE state='queued' ORDER BY tenant ASC, priority DESC, id ASC"
            ).fetchall()
            best_per_tenant: dict[str, sqlite3.Row] = {}
            for row in candidates:
                best_per_tenant.setdefault(row["tenant"], row)
            eligible = [
                row
                for t, row in best_per_tenant.items()
                if running_by_tenant.get(t, 0) < per_tenant_cap
            ]
            if not eligible:
                return None
            # Max-min fair: fewest running for the tenant, then priority, then oldest id.
            chosen = min(
                eligible,
                key=lambda r: (running_by_tenant.get(r["tenant"], 0), -r["priority"], r["id"]),
            )
            conn.execute(
                "UPDATE admission_queue SET state='running', started_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (chosen["id"],),
            )
            return dict(chosen)

    return write_retry(_claim)


def complete_admission(item_id: int, *, state: str = "done", reason: str | None = None) -> None:
    """Mark an admission item finished (``done`` | ``failed`` | ``rejected``)."""

    def _done() -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE admission_queue SET state=?, finished_at=CURRENT_TIMESTAMP, reason=? "
                "WHERE id=?",
                (state, reason, item_id),
            )

    write_retry(_done)


def admission_stats() -> dict[str, Any]:
    """Counts per state, plus how long the oldest queued item has been waiting.

    Counts alone cannot tell a busy queue from a **stranded** one. This facade does not dispatch:
    ``worker_step`` takes an injected ``dispatch`` and something has to call it, so an item
    submitted where nothing drains waits forever — and ``{"queued": 1}`` looks exactly like a queue
    that is simply busy this second. ``oldest_queued_age_s`` is the number that distinguishes them;
    it is ``None`` when nothing is queued.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS c FROM admission_queue GROUP BY state"
        ).fetchall()
        waiting = conn.execute(
            "SELECT MIN(enqueued_at) AS oldest FROM admission_queue WHERE state='queued'"
        ).fetchone()
        age = None
        if waiting and waiting["oldest"]:
            age_row = conn.execute(
                "SELECT CAST(strftime('%s', CURRENT_TIMESTAMP) AS INTEGER) "
                "- CAST(strftime('%s', ?) AS INTEGER) AS age",
                (waiting["oldest"],),
            ).fetchone()
            age = max(0, int(age_row["age"] or 0))
    out: dict[str, Any] = {"queued": 0, "running": 0, "done": 0, "rejected": 0, "failed": 0}
    for r in rows:
        out[r["state"]] = r["c"]
    out["oldest_queued_age_s"] = age
    return out

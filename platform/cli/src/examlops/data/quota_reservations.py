"""examlops.data.quota_reservations - two-phase quota reservation storage (ADR 0116 decision 3).

A reservation holds GPUs (concurrency) and GPU-hours against a project *at admission*, decoupled
from placement. Lifecycle::

    reserved --commit--> committed --release--> released
        |                                          ^
        +--------------------release---------------+
        +--(ttl elapsed, swept)--> expired

``reserved`` (unexpired) and ``committed`` rows hold quota. A ``reserved`` row whose TTL has
elapsed holds nothing the instant it lapses, whether or not a sweep has run; the sweep only makes
the leak *visible* by moving it to ``expired``.

Atomicity: the headroom check and the insert run under one scoped write lock
(``_immediate_write("quota_reservations")`` - SQLite RESERVED lock, Postgres advisory lock), so two
concurrent reservers cannot both take the last slot. Policy (which limits, audit) lives in
:mod:`examlops.admission_seam.reservations`; this module only touches ``quota_reservations`` and
reads the ``admission_queue`` running counts.
"""

from __future__ import annotations

import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = [
    "admission_running_counts",
    "commit",
    "expire_due",
    "get",
    "held_totals",
    "held_gpus_by_tenant",
    "list_reservations",
    "release",
    "reserve",
]

_HOLDING = "(state = 'committed' OR (state = 'reserved' AND expires_at > ?))"


def _held(conn: Any, project: str, now: float) -> tuple[int, float]:
    row = conn.execute(
        "SELECT COALESCE(SUM(gpus), 0) AS g, COALESCE(SUM(gpu_hours), 0) AS h "
        f"FROM quota_reservations WHERE project = ? AND {_HOLDING}",
        (project, now),
    ).fetchone()
    return int(row["g"]), float(row["h"])


def held_totals(project: str, *, now: float | None = None) -> dict[str, float]:
    """GPUs and GPU-hours currently held against ``project``."""
    init_db()
    with get_db() as conn:
        g, h = _held(conn, project, time.time() if now is None else now)
    return {"gpus": g, "gpu_hours": h}


def held_gpus_by_tenant(*, now: float | None = None) -> dict[str, int]:
    """GPUs held per tenant across every project (input to the admission cluster state)."""
    init_db()
    ts = time.time() if now is None else now
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tenant, COALESCE(SUM(gpus), 0) AS g FROM quota_reservations "
            f"WHERE {_HOLDING} GROUP BY tenant",
            (ts,),
        ).fetchall()
    return {r["tenant"]: int(r["g"]) for r in rows}


def reserve(
    reservation_id: str,
    project: str,
    *,
    tenant: str = "default",
    gpus: int = 0,
    gpu_hours: float = 0.0,
    ttl_s: float,
    gpus_limit: int | None = None,
    gpu_hours_limit: float | None = None,
    holder: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically take ``gpus`` / ``gpu_hours`` of a project's headroom.

    Returns ``{"ok": True, "reservation": {...}}`` or ``{"ok": False, "reason": ...}`` naming the
    limit that would have been exceeded. A ``None`` limit means "not limited on that axis".
    Lapsed ``reserved`` rows are swept to ``expired`` first, in the same transaction.
    """
    ts = time.time() if now is None else now

    def _do() -> dict[str, Any]:
        init_db()
        with _immediate_write("quota_reservations") as conn:
            conn.execute(
                "UPDATE quota_reservations SET state = 'expired', resolved_at = ?, "
                "reason = 'ttl elapsed' WHERE state = 'reserved' AND expires_at <= ?",
                (ts, ts),
            )
            g, h = _held(conn, project, ts)
            if gpus_limit is not None and g + gpus > gpus_limit:
                return {
                    "ok": False,
                    "reason": f"gpu concurrency: held {g} + requested {gpus} > limit {gpus_limit}",
                }
            if gpu_hours_limit is not None and h + gpu_hours > gpu_hours_limit + 1e-9:
                return {
                    "ok": False,
                    "reason": (
                        f"gpu-hours: held {h:.2f} + requested {gpu_hours:.2f} "
                        f"> headroom {gpu_hours_limit:.2f}"
                    ),
                }
            conn.execute(
                "INSERT INTO quota_reservations "
                "(id, project, tenant, gpus, gpu_hours, state, holder, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?)",
                (reservation_id, project, tenant, gpus, gpu_hours, holder, ts, ts + ttl_s),
            )
        return {"ok": True, "reservation": get(reservation_id)}

    return write_retry(_do)


def commit(reservation_id: str, *, now: float | None = None) -> bool:
    """reserved -> committed. False if it is not (or is no longer) an unexpired reservation."""
    ts = time.time() if now is None else now

    def _do() -> bool:
        init_db()
        with _immediate_write("quota_reservations") as conn:
            cur = conn.execute(
                "UPDATE quota_reservations SET state = 'committed', resolved_at = ? "
                "WHERE id = ? AND state = 'reserved' AND expires_at > ?",
                (ts, reservation_id, ts),
            )
            return bool(cur.rowcount == 1)

    return write_retry(_do)


def release(reservation_id: str, *, reason: str | None = None, now: float | None = None) -> bool:
    """reserved | committed -> released (completion, failure or cancellation). Idempotent-safe:
    a second release of the same reservation returns False and changes nothing."""
    ts = time.time() if now is None else now

    def _do() -> bool:
        init_db()
        with _immediate_write("quota_reservations") as conn:
            cur = conn.execute(
                "UPDATE quota_reservations SET state = 'released', resolved_at = ?, reason = ? "
                "WHERE id = ? AND state IN ('reserved', 'committed')",
                (ts, reason, reservation_id),
            )
            return bool(cur.rowcount == 1)

    return write_retry(_do)


def expire_due(*, now: float | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    """Lapsed ``reserved`` rows. ``dry_run`` only lists them; otherwise they become ``expired``."""
    ts = time.time() if now is None else now
    init_db()
    if dry_run:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM quota_reservations WHERE state = 'reserved' AND expires_at <= ? "
                "ORDER BY expires_at",
                (ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _do() -> list[dict[str, Any]]:
        with _immediate_write("quota_reservations") as conn:
            rows = conn.execute(
                "SELECT * FROM quota_reservations WHERE state = 'reserved' AND expires_at <= ? "
                "ORDER BY expires_at",
                (ts,),
            ).fetchall()
            conn.execute(
                "UPDATE quota_reservations SET state = 'expired', resolved_at = ?, "
                "reason = 'ttl elapsed' WHERE state = 'reserved' AND expires_at <= ?",
                (ts, ts),
            )
        return [dict(r) for r in rows]

    return write_retry(_do)


def get(reservation_id: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM quota_reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
    return dict(row) if row else None


def list_reservations(
    *, state: str | None = None, project: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    init_db()
    where, params = [], []
    if state:
        where.append("state = ?")
        params.append(state)
    if project:
        where.append("project = ?")
        params.append(project)
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM quota_reservations {clause}ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in rows]


def admission_running_counts() -> dict[str, Any]:
    """``{total, by_tenant}`` of ``admission_queue`` items in state ``running`` (read-only)."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tenant, COUNT(*) AS c FROM admission_queue WHERE state = 'running' "
            "GROUP BY tenant"
        ).fetchall()
    by_tenant = {r["tenant"]: int(r["c"]) for r in rows}
    return {"total": sum(by_tenant.values()), "by_tenant": by_tenant}

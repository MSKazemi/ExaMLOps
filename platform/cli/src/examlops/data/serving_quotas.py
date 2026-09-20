"""examlops.data.serving_quotas — per-tenant request quotas for the serving gateway (ADR 0123 d3).

This module only touches ``serving_quotas``. A change enqueues ``serving.quota_changed`` in the
same transaction, which makes the control-plane projector recompile the serving snapshot
(:mod:`examlops.serving_snapshot`); the gateway enforces what the snapshot carries and never reads
this table itself.
"""

from __future__ import annotations

from typing import Any

from examlops.data.events import enqueue_event
from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = ["get_quota", "list_quotas", "remove_quota", "set_quota"]


def _tenant(tenant: str) -> str:
    name = (tenant or "").strip()
    if not name:
        raise ValueError("a tenant name is required")
    return name


def set_quota(tenant: str, rpm: int, *, updated_by: str | None = None) -> str:
    """Cap ``tenant`` at ``rpm`` requests per minute (0 = unlimited). ``created`` or ``updated``."""
    name = _tenant(tenant)
    if isinstance(rpm, bool) or not isinstance(rpm, int) or rpm < 0:
        raise ValueError(f"rpm must be a non-negative integer, got {rpm!r}")

    def _do() -> str:
        init_db()
        with _immediate_write("serving_quotas") as conn:
            cur = conn.execute(
                "UPDATE serving_quotas SET rpm=?, updated_by=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant=?",
                (rpm, updated_by, name),
            )
            result = "updated"
            if cur.rowcount != 1:
                conn.execute(
                    "INSERT INTO serving_quotas (tenant, rpm, updated_by) VALUES (?,?,?)",
                    (name, rpm, updated_by),
                )
                result = "created"
            enqueue_event(
                "serving.quota_changed",
                {"tenant": name, "rpm": rpm, "removed": False},
                conn=conn,
                actor=updated_by,
            )
            return result

    return write_retry(_do)


def remove_quota(tenant: str, *, updated_by: str | None = None) -> bool:
    """Drop ``tenant``'s override (it falls back to the gateway default). False if none existed."""
    name = _tenant(tenant)

    def _do() -> bool:
        init_db()
        with _immediate_write("serving_quotas") as conn:
            cur = conn.execute("DELETE FROM serving_quotas WHERE tenant=?", (name,))
            if cur.rowcount < 1:
                return False
            enqueue_event(
                "serving.quota_changed",
                {"tenant": name, "rpm": None, "removed": True},
                conn=conn,
                actor=updated_by,
            )
            return True

    return write_retry(_do)


def get_quota(tenant: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT tenant, rpm, updated_by, updated_at FROM serving_quotas WHERE tenant=?",
            (_tenant(tenant),),
        ).fetchone()
    return dict(row) if row else None


def list_quotas() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tenant, rpm, updated_by, updated_at FROM serving_quotas ORDER BY tenant"
        ).fetchall()
    return [dict(r) for r in rows]

"""``examlops.audit`` — the platform audit log through the stable SDK (ADR 0078 clause 1).

Read-only. Every filter — tenant included — is a SQL predicate evaluated **before** the
``LIMIT``, so a filtered query never returns fewer rows than exist merely because other tenants'
events filled the window. ``limit`` is capped at :data:`MAX_LIMIT`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from examlops.sdk.errors import InvalidArgumentError, UnavailableError

__all__ = ["AuditEvent", "MAX_LIMIT", "query"]

#: The largest page :func:`query` returns in one call.
MAX_LIMIT = 10_000


@dataclass(frozen=True)
class AuditEvent:
    """One hash-chained audit event. ``details`` is the decoded JSON payload (or ``None``)."""

    id: int
    ts: str
    source: str | None
    actor: str | None
    action: str
    target: str | None
    details: Any
    tenant: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw  # a pre-JSON row: return it as recorded rather than dropping it


def query(
    *,
    since_days: int = 30,
    model: str | None = None,
    action: str | None = None,
    source: str | None = None,
    actor: str | None = None,
    tenant: str | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    """Audit events newest first (``ts`` then ``id``, the chain's own order), filtered in SQL.

    ``model`` matches the event target exactly. ``tenant=None`` reads every tenant.
    """
    if isinstance(since_days, bool) or not isinstance(since_days, int) or since_days < 0:
        raise InvalidArgumentError("since_days must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidArgumentError("limit must be a positive integer")
    limit = min(limit, MAX_LIMIT)
    since = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")

    sql = (
        "SELECT id, ts, source, actor, action, target, details, tenant "
        "FROM audit_events WHERE ts >= ?"
    )
    params: list[Any] = [since]
    for column, value in (
        ("target", model),
        ("action", action),
        ("source", source),
        ("actor", actor),
    ):
        if value:
            sql += f" AND {column}=?"
            params.append(value)
    if tenant:
        # `tenant` defaults to 'default' on write; a NULL from a pre-tenancy row is the default.
        sql += " AND COALESCE(tenant, 'default')=?"
        params.append(tenant)
    # `id` breaks the one-second `ts` ties in the chain's own order (see `exa audit`).
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)

    from examlops.data import get_db, init_db

    try:
        init_db()
        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001 - the datastore's own error types are private
        raise UnavailableError(f"audit log unavailable: {exc}") from exc
    return [
        AuditEvent(
            id=int(r["id"]),
            ts=str(r["ts"]),
            source=r["source"],
            actor=r["actor"],
            action=str(r["action"]),
            target=r["target"],
            details=_decode(r["details"]),
            tenant=str(r["tenant"] or "default"),
        )
        for r in rows
    ]

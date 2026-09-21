"""examlops.data.suspend - storage for suspend/resume snapshots (ADR 0109).

Policy (backends, capability honesty, cost model) lives in :mod:`examlops.suspend`; this module
only touches ``suspend_snapshots``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = ["get", "init_db", "list_snapshots", "mark", "put", "recorded_restores"]

_COLS = (
    "snapshot_id, ts, backend, subject_kind, subject_id, tenant, status, pointer, state_bytes, "
    "capability, actor, resumed_at, state_transfer_s, communicator_rebuild_s"
)


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("pointer", "capability"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def put(
    snapshot_id: str,
    backend: str,
    subject_kind: str,
    subject_id: str,
    *,
    tenant: str = "default",
    status: str = "suspended",
    pointer: dict[str, Any] | None = None,
    state_bytes: int | None = None,
    capability: dict[str, Any] | None = None,
    actor: str | None = None,
) -> None:
    def _do() -> None:
        init_db()
        with _immediate_write("suspend_snapshots") as conn:
            conn.execute(
                f"INSERT INTO suspend_snapshots ({_COLS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    snapshot_id,
                    time.time(),
                    backend,
                    subject_kind,
                    subject_id,
                    tenant,
                    status,
                    json.dumps(pointer or {}),
                    state_bytes,
                    json.dumps(capability or {}),
                    actor,
                ),
            )

    write_retry(_do)


def mark(
    snapshot_id: str,
    status: str,
    *,
    state_transfer_s: float | None = None,
    communicator_rebuild_s: float | None = None,
) -> bool:
    """Set a snapshot's status. Returns whether a row was updated."""

    def _do() -> bool:
        init_db()
        resumed = time.time() if status == "resumed" else None
        with _immediate_write("suspend_snapshots") as conn:
            cur = conn.execute(
                "UPDATE suspend_snapshots SET status=?, resumed_at=COALESCE(?, resumed_at), "
                "state_transfer_s=COALESCE(?, state_transfer_s), "
                "communicator_rebuild_s=COALESCE(?, communicator_rebuild_s) WHERE snapshot_id=?",
                (status, resumed, state_transfer_s, communicator_rebuild_s, snapshot_id),
            )
            return cur.rowcount == 1

    return write_retry(_do)


def get(snapshot_id: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM suspend_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return _row(row) if row else None

    return write_retry(_do)


def list_snapshots(
    subject_id: str | None = None, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        where, args = [], []
        if subject_id:
            where.append("subject_id=?")
            args.append(subject_id)
        if status:
            where.append("status=?")
            args.append(status)
        sql = f"SELECT {_COLS} FROM suspend_snapshots"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, snapshot_id LIMIT ?"
        with get_db() as conn:
            return [_row(r) for r in conn.execute(sql, [*args, int(limit)])]

    return write_retry(_do)


def recorded_restores(backend: str) -> list[dict[str, Any]]:
    """Restores that recorded a transfer time - the only source of a ``measured`` basis."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT state_bytes, state_transfer_s FROM suspend_snapshots "
                    "WHERE backend=? AND status='resumed' AND state_transfer_s IS NOT NULL",
                    (backend,),
                )
            ]

    return write_retry(_do)

"""examlops.data.audit_anchors - storage for transparency receipts and maintenance runs (ADR 0028).

The data layer behind :mod:`examlops.audit_transparency` (one receipt per ``(backend,
head_hash)``, so a re-run never logs twice) and :mod:`examlops.audit_maintenance` (the schedule's
bounded run history). Both tables are additive (``platform_db`` DDL).
"""

from __future__ import annotations

import json
from typing import Any

from examlops.platform_db import get_db, init_db, write_retry

__all__ = [
    "count_checkpoints_after",
    "get_transparency_receipt",
    "list_maintenance_runs",
    "list_transparency_receipts",
    "record_maintenance_run",
    "store_transparency_receipt",
]


def get_transparency_receipt(head_hash: str, backend: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM audit_transparency_entries WHERE backend = ? AND head_hash = ?",
            (backend, head_hash),
        ).fetchone()
    return dict(row) if row else None


def list_transparency_receipts(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(int(limit), 10_000))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_transparency_entries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def store_transparency_receipt(row: dict[str, Any]) -> None:
    """Insert a receipt; a second one for the same ``(backend, head_hash)`` is ignored."""
    init_db()

    def _write() -> None:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_transparency_entries (head_id, head_hash, backend, log_url, "
                "entry_uuid, log_index, integrated_time, statement_sha256, key_id, receipt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (backend, head_hash) DO NOTHING",
                (
                    row["head_id"],
                    row["head_hash"],
                    row["backend"],
                    row.get("log_url"),
                    row.get("entry_uuid"),
                    row.get("log_index"),
                    row.get("integrated_time"),
                    row["statement_sha256"],
                    row.get("key_id"),
                    json.dumps(row["receipt"], sort_keys=True),
                ),
            )

    write_retry(_write)


def count_checkpoints_after(head_id: int) -> int:
    """Distinct signed checkpoint heads newer than ``head_id``."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT head_hash) AS n FROM audit_checkpoints WHERE head_id > ?",
            (int(head_id),),
        ).fetchone()
    return int(row["n"] or 0)


def record_maintenance_run(holder: str, status: str, result: dict[str, Any], *, keep: int) -> None:
    """Append one run and trim the history to the newest ``keep`` rows (bounded heartbeat)."""
    init_db()

    def _write() -> None:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_maintenance_runs (holder, status, result) VALUES (?,?,?)",
                (holder, status, json.dumps(result, default=str, sort_keys=True)),
            )
            conn.execute(
                "DELETE FROM audit_maintenance_runs WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) FROM audit_maintenance_runs) - ?",
                (int(keep),),
            )

    write_retry(_write)


def list_maintenance_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Recorded runs, newest first, with ``result`` decoded from JSON."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_maintenance_runs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["result"] = json.loads(d["result"])
        except (TypeError, ValueError):
            pass
        out.append(d)
    return out

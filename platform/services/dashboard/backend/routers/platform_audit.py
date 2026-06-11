"""Platform audit log — reads audit_events from shared platform.db."""
from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/platform-audit", tags=["platform-audit"])

_admin = require_role("admin")
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("")
async def get_platform_audit(
    _=Depends(_admin),
    last_days: int = Query(30, ge=1, le=365),
    model: str | None = Query(None),
    action: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict]:
    """Read platform audit_events from shared platform.db."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        query = (
            "SELECT id, ts, source, actor, action, target, details "
            "FROM audit_events WHERE ts >= datetime('now', ?) "
        )
        params: list = [f"-{last_days} days"]
        if model:
            query += " AND target=?"
            params.append(model)
        if action:
            query += " AND action=?"
            params.append(action)
        if source:
            query += " AND source=?"
            params.append(source)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [
            {
                "id": r["id"], "ts": r["ts"], "source": r["source"],
                "actor": r["actor"], "action": r["action"], "target": r["target"],
                "details": json.loads(r["details"]) if r["details"] else None,
            }
            for r in rows
        ]
    except Exception:
        return []

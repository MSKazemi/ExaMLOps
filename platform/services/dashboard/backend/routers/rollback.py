"""Rollback router — reads model_rollbacks from shared platform.db."""

from __future__ import annotations

import os

from auth import require_role
from dbconn import connect
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/rollback", tags=["rollback"])

_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/history/{model}")
async def get_rollback_history(model: str, _=Depends(_viewer)) -> list[dict]:
    """Return the last 20 rollback events for *model* from platform.db."""
    try:
        conn = connect(_db_path())
        # Table may not exist on fresh installs — handle gracefully
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_rollbacks ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "  model        TEXT NOT NULL,"
            "  from_version INTEGER,"
            "  to_version   INTEGER NOT NULL,"
            "  alias        TEXT NOT NULL DEFAULT 'Production',"
            "  actor        TEXT,"
            "  reason       TEXT"
            ")"
        )
        rows = conn.execute(
            "SELECT id, ts, model, from_version, to_version, alias, actor, reason "
            "FROM model_rollbacks WHERE model=? ORDER BY ts DESC LIMIT 20",
            (model.upper(),),
        ).fetchall()
        conn.close()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "model": r["model"],
                "from_version": r["from_version"],
                "to_version": r["to_version"],
                "alias": r["alias"],
                "actor": r["actor"],
                "reason": r["reason"],
            }
            for r in rows
        ]
    except Exception:
        return []

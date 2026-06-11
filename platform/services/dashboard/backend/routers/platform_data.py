"""Traffic rules and promotion rules from shared platform.db."""
from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/platform-data", tags=["platform-data"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/traffic-rules")
async def get_all_traffic_rules(_=Depends(_viewer)) -> list[dict]:
    """All traffic split rules."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT model, rules, updated_at, updated_by FROM traffic_rules ORDER BY model"
        ).fetchall()
        conn.close()
        return [
            {"model": r["model"], "rules": json.loads(r["rules"]),
             "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
            for r in rows
        ]
    except Exception:
        return []


@router.get("/traffic-rules/{model}")
async def get_model_traffic_rules(model: str, _=Depends(_viewer)) -> dict | None:
    """Traffic split rules for one model."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT model, rules, updated_at, updated_by FROM traffic_rules WHERE model=?",
            (model,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {"model": row["model"], "rules": json.loads(row["rules"]),
                "updated_at": row["updated_at"], "updated_by": row["updated_by"]}
    except Exception:
        return None


@router.get("/promotion-rules")
async def get_all_promotion_rules(_=Depends(_viewer)) -> list[dict]:
    """All metric-gated promotion rules."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM promotion_rules ORDER BY model").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

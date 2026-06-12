"""Data quality gates — history endpoint."""
from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/quality", tags=["quality"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/history/{model}")
async def get_quality_history(model: str, _=Depends(_viewer)) -> list[dict]:
    """Return the last 20 data quality check results for *model*."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, ts, model, dataset, status, passed, failed, details_json, actor
               FROM data_quality_checks
               WHERE model=?
               ORDER BY ts DESC
               LIMIT 20""",
            (model,),
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            item: dict = dict(r)
            if item.get("details_json"):
                try:
                    item["details"] = json.loads(item["details_json"])
                except Exception:
                    item["details"] = []
            else:
                item["details"] = []
            del item["details_json"]
            result.append(item)
        return result
    except Exception:
        return []

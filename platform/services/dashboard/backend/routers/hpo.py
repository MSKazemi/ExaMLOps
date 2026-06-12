"""HPO studies and trials read-only router."""

from __future__ import annotations

import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/hpo", tags=["hpo"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/studies")
async def get_studies(_=Depends(_viewer)) -> list[dict]:
    """Last 50 HPO studies, most recent first."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, ts, model, dataset, n_trials, metric, status,"
            "       flow_run_id, best_params_json, best_value, actor"
            " FROM hpo_studies ORDER BY ts DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@router.get("/trials/{study_id}")
async def get_trials(study_id: int, _=Depends(_viewer)) -> list[dict]:
    """All HPO trials for a given study."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, study_id, ts, trial_num, params_json, value"
            " FROM hpo_trials WHERE study_id=? ORDER BY trial_num",
            (study_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

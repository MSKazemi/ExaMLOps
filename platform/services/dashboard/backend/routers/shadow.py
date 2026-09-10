"""Shadow deployment — reads shadow_config and shadow_results from platform.db."""

from __future__ import annotations

from auth import require_role
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/shadow", tags=["shadow"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return platform_db_path()


@router.get("/config")
async def get_shadow_config(_=Depends(_viewer)) -> list[dict]:
    """Return all shadow deployment configuration rows."""
    try:
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT model, shadow_alias, enabled, updated_at, updated_by FROM shadow_config ORDER BY model"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


@router.get("/results/{model}")
async def get_shadow_results(model: str, _=Depends(_viewer)) -> list[dict]:
    """Return the last 50 shadow inference comparison results for a model."""
    try:
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                """SELECT id, ts, model, production_pred, shadow_pred, diff_pct, job_id
               FROM shadow_results
               WHERE model=?
               ORDER BY ts DESC, id DESC
               LIMIT 50""",
                (model,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []

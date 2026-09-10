"""Explain-log read-only router — XAI history from shared platform.db."""

from __future__ import annotations

from auth import require_role
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/explain", tags=["explain"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return platform_db_path()


def _rows_from_db(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read query against the platform SQLite DB and return row dicts."""
    try:
        conn = connect(_db_path())
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


@router.get("/history/{model}")
async def get_explain_history(model: str, _=Depends(_viewer)) -> list[dict]:
    """Last 20 explain requests for a model."""
    return _rows_from_db(
        "SELECT id, ts, model, alias, input_hash, top_n, status, error "
        "FROM explain_logs WHERE model=? ORDER BY ts DESC LIMIT 20",
        (model,),
    )


@router.get("/summary")
async def get_explain_summary(_=Depends(_viewer)) -> list[dict]:
    """Count of explain requests per model."""
    return _rows_from_db(
        "SELECT model, COUNT(*) AS total, "
        "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count, "
        "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS error_count, "
        "MAX(ts) AS last_ts "
        "FROM explain_logs GROUP BY model ORDER BY model",
    )

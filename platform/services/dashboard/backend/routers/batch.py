"""Batch inference job history router (viewer-readable)."""
from __future__ import annotations

import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/batch", tags=["batch"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


@router.get("/jobs")
async def get_all_batch_jobs(_=Depends(_viewer)) -> list[dict]:
    """Last 50 batch inference jobs, most recent first."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ts, model, alias, input_path, output_path,
                   n_inputs, n_success, n_errors, elapsed_s, actor
            FROM batch_jobs
            ORDER BY ts DESC
            LIMIT 50
            """
        ).fetchall()
        conn.close()
        return _rows_to_dicts(rows)
    except Exception:
        return []


@router.get("/jobs/{model}")
async def get_model_batch_jobs(model: str, _=Depends(_viewer)) -> list[dict]:
    """Last 50 batch inference jobs for a specific model, most recent first."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ts, model, alias, input_path, output_path,
                   n_inputs, n_success, n_errors, elapsed_s, actor
            FROM batch_jobs
            WHERE model = ?
            ORDER BY ts DESC
            LIMIT 50
            """,
            (model,),
        ).fetchall()
        conn.close()
        return _rows_to_dicts(rows)
    except Exception:
        return []

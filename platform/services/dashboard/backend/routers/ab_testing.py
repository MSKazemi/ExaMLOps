"""A/B testing router — exposes ab_tests and ab_results from shared platform.db."""

from __future__ import annotations

import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/ab-testing", tags=["ab-testing"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/tests")
async def list_tests(_=Depends(_viewer)) -> list[dict]:
    """Return the last 50 A/B tests ordered by start time (newest first)."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, model, name, variant_a, variant_b, split_pct, status, "
            "started_at, ended_at, created_by "
            "FROM ab_tests ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Table not yet created — return empty list gracefully
        return []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/results/{test_id}")
async def get_results(test_id: int, _=Depends(_viewer)) -> dict:
    """Return raw ab_results rows for a test plus mean/count per variant."""
    try:
        conn = _connect()

        # Verify the test exists
        test_row = conn.execute(
            "SELECT id, model, name, variant_a, variant_b, split_pct, status, started_at, ended_at "
            "FROM ab_tests WHERE id=?",
            (test_id,),
        ).fetchone()
        if test_row is None:
            conn.close()
            raise HTTPException(status_code=404, detail=f"A/B test {test_id} not found")

        # All individual observations
        result_rows = conn.execute(
            "SELECT id, ts, variant, value FROM ab_results WHERE test_id=? ORDER BY ts",
            (test_id,),
        ).fetchall()

        # Aggregate: mean and count per variant
        agg_rows = conn.execute(
            "SELECT variant, COUNT(*) AS count, AVG(value) AS mean "
            "FROM ab_results WHERE test_id=? GROUP BY variant",
            (test_id,),
        ).fetchall()
        conn.close()

        aggregates = {r["variant"]: {"count": r["count"], "mean": r["mean"]} for r in agg_rows}

        return {
            "test": dict(test_row),
            "aggregates": aggregates,
            "results": [dict(r) for r in result_rows],
        }
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        return {"test": None, "aggregates": {}, "results": []}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

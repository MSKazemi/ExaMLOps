"""Model card history router — reads model_cards from shared platform.db."""

from __future__ import annotations

import sqlite3

from auth import require_role
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/cards", tags=["cards"])

_viewer = require_role("viewer")


def _db_path() -> str:
    return platform_db_path()


def _get_conn() -> sqlite3.Connection:
    conn = connect(_db_path())
    return conn


@router.get("/history")
async def get_card_history(_=Depends(_viewer)) -> list[dict]:
    """Return the last 50 model card generation records."""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, ts, model, output_path, actor FROM model_cards ORDER BY ts DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@router.get("/{model}/latest")
async def get_latest_card(model: str, _=Depends(_viewer)) -> dict:
    """Return the most recent model card record for a given model."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, ts, model, output_path, actor "
            "FROM model_cards WHERE model=? ORDER BY ts DESC LIMIT 1",
            (model,),
        ).fetchone()
        conn.close()
        if row is None:
            return {}
        return dict(row)
    except Exception:
        return {}

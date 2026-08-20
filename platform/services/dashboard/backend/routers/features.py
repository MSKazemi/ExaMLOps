"""Feature store data — reads from shared platform.db feature_versions table."""

from __future__ import annotations

import os
import sqlite3

from auth import require_role
from dbconn import connect
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/features", tags=["features"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            model       TEXT NOT NULL,
            name        TEXT NOT NULL DEFAULT 'default',
            version     INTEGER NOT NULL DEFAULT 1,
            local_path  TEXT,
            size_bytes  INTEGER,
            schema_json TEXT,
            actor       TEXT
        )
    """)


@router.get("/versions")
async def list_versions(_=Depends(_viewer)) -> list[dict]:
    """Return the last 50 feature versions across all models."""
    conn = connect(_db_path())
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, ts, model, name, version, local_path, size_bytes, schema_json, actor"
            " FROM feature_versions ORDER BY ts DESC, id DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/versions/{model}")
async def list_versions_for_model(model: str, _=Depends(_viewer)) -> list[dict]:
    """Return feature versions for a specific model."""
    conn = connect(_db_path())
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT id, ts, model, name, version, local_path, size_bytes, schema_json, actor"
            " FROM feature_versions WHERE model=? ORDER BY ts DESC, id DESC LIMIT 50",
            (model,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()

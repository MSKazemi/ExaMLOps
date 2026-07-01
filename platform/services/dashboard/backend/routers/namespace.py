"""Namespace (multi-tenancy) — reads namespace / namespace_models from shared platform.db."""

from __future__ import annotations

import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/namespaces", tags=["namespaces"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS namespaces (
            name        TEXT PRIMARY KEY,
            description TEXT,
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by  TEXT
        );
        CREATE TABLE IF NOT EXISTS namespace_models (
            model       TEXT NOT NULL,
            namespace   TEXT NOT NULL DEFAULT 'default',
            assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (model, namespace)
        );
    """)


@router.get("")
async def get_namespaces(_=Depends(_viewer)) -> list[dict]:
    """All namespaces with the number of models assigned to each."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        _ensure_tables(conn)
        # Ensure default namespace always present
        conn.execute("INSERT OR IGNORE INTO namespaces (name) VALUES ('default')")
        conn.commit()
        rows = conn.execute(
            "SELECT name, description, created_at, created_by FROM namespaces ORDER BY name"
        ).fetchall()
        result = []
        for row in rows:
            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM namespace_models WHERE namespace=?",
                (row["name"],),
            ).fetchone()
            result.append(
                {
                    "name": row["name"],
                    "description": row["description"],
                    "created_at": row["created_at"],
                    "created_by": row["created_by"],
                    "model_count": count_row["cnt"] if count_row else 0,
                }
            )
        conn.close()
        return result
    except Exception:
        return []


@router.get("/{name}/models")
async def get_namespace_models(name: str, _=Depends(_viewer)) -> dict:
    """All models assigned to a specific namespace."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        _ensure_tables(conn)
        ns_row = conn.execute(
            "SELECT name, description, created_at, created_by FROM namespaces WHERE name=?",
            (name,),
        ).fetchone()
        if not ns_row:
            conn.close()
            return {"error": f"Namespace '{name}' not found"}
        model_rows = conn.execute(
            "SELECT model, assigned_at FROM namespace_models WHERE namespace=? ORDER BY model",
            (name,),
        ).fetchall()
        conn.close()
        return {
            "name": ns_row["name"],
            "description": ns_row["description"],
            "created_at": ns_row["created_at"],
            "created_by": ns_row["created_by"],
            "models": [{"model": r["model"], "assigned_at": r["assigned_at"]} for r in model_rows],
        }
    except Exception:
        return {"error": "internal error"}

"""Named Connections (ADR 0087) — read-only dashboard surface over platform.db.

A Connection is a reusable, project-scoped data endpoint (s3 / uri / dataplane). Non-secret config
lives in ``platform.db``; the actual credential lives only in the secrets client, referenced by
``secret_ref`` (never the value). This router is **read-only**: it lists connections and reports
whether a secret is attached, but never returns a secret value and never creates one — creating a
connection with a credential is a CLI-only operation (``exa connection create``) so the secret is
written through ``examlops.secrets`` rather than this raw-sqlite path.
"""

from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/v1/connections", tags=["connections"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS connections (
               name TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
               config_json TEXT NOT NULL DEFAULT '{}', secret_ref TEXT,
               created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by TEXT,
               PRIMARY KEY (project, name)
           )"""
    )


def _row_to_view(r: sqlite3.Row) -> dict:
    """Shape a connection row for the UI. Never exposes a secret value — only ``hasSecret``."""
    try:
        config = json.loads(r["config_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        config = {}
    # Defence in depth: strip any credential-looking keys that should never live in config_json.
    for k in list(config):
        if (
            any(s in k.lower() for s in ("secret", "password", "token", "key"))
            and "access" not in k.lower()
        ):
            config[k] = "***"
    return {
        "name": r["name"],
        "project": r["project"] or None,
        "kind": r["kind"],
        "config": config,
        "hasSecret": bool(r["secret_ref"]),
        "createdAt": r["created_at"],
        "createdBy": r["created_by"],
    }


@router.get("")
async def list_connections_view(
    project: str | None = Query(default=None),
    _=Depends(_viewer),
) -> list[dict]:
    """List connections (optionally filtered by project). Viewer. Never returns secret values."""
    try:
        conn = _connect()
        _ensure_table(conn)
        if project is not None:
            rows = conn.execute(
                "SELECT * FROM connections WHERE project=? ORDER BY name", (project,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM connections ORDER BY project, name").fetchall()
        out = [_row_to_view(r) for r in rows]
        conn.close()
        return out
    except Exception:
        return []

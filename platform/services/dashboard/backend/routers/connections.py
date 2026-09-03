"""Named Connections (ADR 0087) — read + config-write dashboard surface over platform.db.

A Connection is a reusable, project-scoped data endpoint (s3 / uri / dataplane). Non-secret config
lives in ``platform.db``; the actual credential lives only in the secrets client, referenced by
``secret_ref`` (never the value).

Reads are viewer-gated and never return a secret value (only ``hasSecret``). **Writes** (create /
delete / test) require the ``connection.manage`` capability (admin) and are audited. Unlike most
dashboard routers, the writes call the *same* ``examlops.connections`` code paths the ``exa
connection create`` CLI uses — so the credential is written through ``examlops.secrets`` (readable by
the CLI and serving layer) and the dashboard can never drift from the CLI. The ``examlops`` import is
lazy + guarded so the dashboard still boots (writes degrade to 503) if the package is absent.
"""

from __future__ import annotations

import json
import os
import sqlite3

import audit_write
from auth import require_role
from capabilities import CONNECTION_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/connections", tags=["connections"])
_viewer = require_role("viewer")
_admin = require_role("admin")

_KINDS = {"s3", "uri", "dataplane"}


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _connect() -> sqlite3.Connection:
    conn = connect(_db_path())
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


def _ensure_audit(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
               source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
           )"""
    )


def _audit(conn: sqlite3.Connection, actor: str, action: str, target: str, details: dict) -> None:
    _ensure_audit(conn)
    audit_write.audit(actor, action, target, details, conn=conn)


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


def _view_one(project: str, name: str) -> dict | None:
    conn = _connect()
    _ensure_table(conn)
    row = conn.execute(
        "SELECT * FROM connections WHERE project=? AND name=?", (project, name)
    ).fetchone()
    out = _row_to_view(row) if row else None
    conn.close()
    return out


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, CONNECTION_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, CONNECTION_MANAGE))


def _examlops_connections():
    """Lazy, guarded import of the shared CLI code path (503 if the package is unavailable)."""
    try:
        from examlops import connections as _c  # type: ignore

        return _c
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "connection writes require the examlops package (not available in this deployment)",
        ) from exc


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
    except sqlite3.OperationalError as exc:
        # A missing table just means nothing was recorded yet (D12); any other
        # datastore failure must surface, not masquerade as an empty list.
        if "no such table" in str(exc).lower():
            return []
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "datastore unavailable") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "datastore unavailable") from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connection_view(
    payload: dict = Body(...), principal: dict = Depends(_admin)
) -> dict:
    """Create a named connection (admin / connection.manage; audited).

    Body: ``{name, kind, project?, config?, secret?}``. A ``secret`` is written through
    ``examlops.secrets`` (CLI-compatible) and only its ``secret_ref`` is kept — never the value.
    Returns the secret-safe view (``hasSecret`` only).
    """
    _require_manage(principal)
    name = (payload.get("name") or "").strip()
    kind = (payload.get("kind") or "").strip()
    project = (payload.get("project") or "").strip()
    config = payload.get("config") or {}
    secret = payload.get("secret")
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    if kind not in _KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"kind must be one of {sorted(_KINDS)}")
    if not isinstance(config, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "config must be an object")
    if _view_one(project, name) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Connection '{name}' already exists (project={project or '-'})",
        )
    conns = _examlops_connections()
    actor = principal.get("sub", "?")
    try:
        conns.create_connection(
            name,
            kind,
            project=project or None,
            config=config,
            secret_value=(str(secret) if secret not in (None, "") else None),
            created_by=actor,
        )
    except conns.ConnectionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # Audit through the dashboard's own connection so it lands in the same DB the UI reads.
    conn = _connect()
    _audit(
        conn,
        actor,
        "connection_created",
        name,
        {"project": project or None, "kind": kind, "hasSecret": bool(secret)},
    )
    conn.commit()
    conn.close()
    view = _view_one(project, name)
    if view is None:  # pragma: no cover - create succeeded but row vanished
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "connection not found after create"
        )
    return view


@router.post("/{name}/test")
async def test_connection_view(
    name: str,
    project: str | None = Query(default=None),
    principal: dict = Depends(_admin),
) -> dict:
    """Reachability probe for a connection (admin / connection.manage). Never returns a secret."""
    _require_manage(principal)
    conns = _examlops_connections()
    try:
        result = conns.test_connection(name, project=(project or None))
    except conns.ConnectionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"name": name, "project": project or None, **result}


@router.delete("/{name}", status_code=status.HTTP_200_OK)
async def delete_connection_view(
    name: str,
    project: str | None = Query(default=None),
    principal: dict = Depends(_admin),
) -> dict:
    """Delete a connection (admin / connection.manage; audited). Does not delete the secret value."""
    _require_manage(principal)
    proj = project or ""
    if _view_one(proj, name) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Connection '{name}' not found (project={proj or '-'})",
        )
    conns = _examlops_connections()
    removed = conns.delete_connection(name, project=(project or None))
    conn = _connect()
    _audit(conn, principal.get("sub", "?"), "connection_deleted", name, {"project": proj or None})
    conn.commit()
    conn.close()
    return {"name": name, "project": proj or None, "deleted": bool(removed)}

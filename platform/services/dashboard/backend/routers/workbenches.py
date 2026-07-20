"""Workbenches (ADR 0090) — dashboard surface over the platform.db ``workbenches`` table.

A workbench is an on-demand, project-bound dev environment. This router lists workbenches (viewer)
and toggles their RUNNING/STOPPED status (admin / project.manage; audited). Status is *intent* —
the actual pod spawn is delegated to the runtime (JupyterHub/Docker), matching the ADR 0084
boundary; the dashboard records and reports intent, it does not spawn containers itself. Creation
and env-var injection (which resolve a project's Named Connections) stay CLI-only.
"""

from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from capabilities import PROJECT_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/workbenches", tags=["workbenches"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _connect() -> sqlite3.Connection:
    conn = connect(_db_path())
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workbenches (
            name TEXT NOT NULL, project TEXT NOT NULL,
            image TEXT NOT NULL DEFAULT 'jupyter/scipy-notebook:latest',
            cpu REAL, memory_gb REAL, storage_volume TEXT,
            status TEXT NOT NULL DEFAULT 'STOPPED',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by TEXT,
            PRIMARY KEY (project, name)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
        );
        """
    )


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, PROJECT_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, PROJECT_MANAGE))


def _examlops_workbenches():
    """Lazy, guarded import of the shared CLI code path (503 if the package is unavailable)."""
    try:
        from examlops import workbenches as _wb  # type: ignore

        return _wb
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "workbench writes require the examlops package (not available in this deployment)",
        ) from exc


def _audit(conn: sqlite3.Connection, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


def _row_to_view(r: sqlite3.Row) -> dict:
    return {
        "name": r["name"],
        "project": r["project"],
        "image": r["image"],
        "cpu": r["cpu"],
        "memoryGb": r["memory_gb"],
        "volume": r["storage_volume"],
        "status": r["status"],
        "createdAt": r["created_at"],
        "createdBy": r["created_by"],
    }


@router.get("")
async def list_workbenches_view(
    project: str | None = Query(default=None),
    _=Depends(_viewer),
) -> list[dict]:
    """List workbenches (optionally scoped to one project). Viewer."""
    try:
        conn = _connect()
        _ensure_table(conn)
        if project is not None:
            rows = conn.execute(
                "SELECT * FROM workbenches WHERE project=? ORDER BY name", (project,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM workbenches ORDER BY project, name").fetchall()
        out = [_row_to_view(r) for r in rows]
        conn.close()
        return out
    except Exception:
        return []


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workbench_view(
    payload: dict = Body(...), principal: dict = Depends(_admin)
) -> dict:
    """Create a project-bound workbench (notebook) — admin / project.manage; audited.

    Body: ``{project, name, image?, cpu?, memoryGb?}``. Reuses ``examlops.workbenches.create_workbench``
    (the same code path as ``exa workbench create``) so the dashboard can never drift from the CLI.
    Registers a per-workbench storage volume and a ``kind='storage'`` project resource. Status STOPPED.
    """
    _require_manage(principal)
    project = (payload.get("project") or "").strip()
    name = (payload.get("name") or "").strip()
    image = (payload.get("image") or "").strip() or None
    if not project:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project is required")
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    wb = _examlops_workbenches()
    actor = principal.get("sub", "?")
    try:
        created = wb.create_workbench(
            name,
            project,
            image=image,
            cpu=payload.get("cpu"),
            memory_gb=payload.get("memoryGb"),
            created_by=actor,
        )
    except wb.WorkbenchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"workbench '{name}' already exists in project '{project}'",
        ) from exc
    conn = _connect()
    _ensure_table(conn)
    _audit(conn, actor, "workbench_created", name, {"project": project, "image": image})
    conn.commit()
    conn.close()
    return {
        "name": created["name"],
        "project": created["project"],
        "image": created["image"],
        "cpu": created["cpu"],
        "memoryGb": created["memory_gb"],
        "volume": created["storage_volume"],
        "status": created["status"],
        "createdAt": created.get("created_at"),
        "createdBy": created.get("created_by"),
    }


@router.post("/{project}/{name}/status")
async def set_status_view(
    project: str,
    name: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Set a workbench's status to RUNNING or STOPPED (admin / project.manage; audited).

    Routes through ``examlops.workbenches`` so RUNNING returns the runtime launch spec — image,
    mounted volume, and the project's Named Connections injected as env vars (secret values are
    resolved server-side and never surfaced key-by-key here beyond the count).
    """
    _require_manage(principal)
    desired = (payload.get("status") or "").upper()
    if desired not in {"RUNNING", "STOPPED"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "status must be RUNNING or STOPPED")
    wb = _examlops_workbenches()
    actor = principal.get("sub", "?")
    result: dict = {"project": project, "name": name, "status": desired}
    try:
        if desired == "RUNNING":
            spec = wb.start_workbench(name, project, actor=actor)
            result["image"] = spec.get("image")
            result["volume"] = spec.get("volume")
            # Surface only the injected env var *names* (not values) so the UI can show wiring.
            result["injectedEnv"] = sorted((spec.get("env") or {}).keys())
        else:
            if not wb.stop_workbench(name, project):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"workbench '{name}' not found in project '{project}'",
                )
    except wb.WorkbenchError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    conn = _connect()
    _ensure_table(conn)
    _audit(conn, actor, "workbench_status_changed", name, {"project": project, "status": desired})
    conn.commit()
    conn.close()
    return result


@router.delete("/{project}/{name}", status_code=status.HTTP_200_OK)
async def delete_workbench_view(
    project: str,
    name: str,
    principal: dict = Depends(_admin),
) -> dict:
    """Delete a workbench definition (admin / project.manage; audited)."""
    _require_manage(principal)
    wb = _examlops_workbenches()
    if not wb.delete_workbench(name, project):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"workbench '{name}' not found in project '{project}'"
        )
    conn = _connect()
    _ensure_table(conn)
    _audit(conn, principal.get("sub", "?"), "workbench_deleted", name, {"project": project})
    conn.commit()
    conn.close()
    return {"project": project, "name": name, "deleted": True}

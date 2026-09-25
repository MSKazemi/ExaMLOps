"""Workbenches (ADR 0090) — dashboard surface over the platform.db ``workbenches`` table.

A workbench is an on-demand, project-bound dev environment. This router lists/creates workbenches
and toggles their RUNNING/STOPPED status (admin / project.manage; audited). RUNNING actually
**spawns a JupyterHub named server** — the dashboard's docker-socket-proxy forbids container
creation, so the Hub (which holds real Docker access and proxies HTTP/WebSocket on 18888) is the
spawner; the dashboard calls its REST API with a service token and returns the Open URL. When the
Hub env is unset the router degrades to intent-only (DB status flip, no spawn), so tests and the
no-Hub dev path keep working.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import urllib.error
import urllib.request

import audit_write
from auth import require_role
from capabilities import PROJECT_MANAGE, can, deny_reason, require_capability
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/workbenches", tags=["workbenches"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return platform_db_path()


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
            -- ADR 0157 Phase 2. Kept in step with examlops.workbenches._ensure_table: this copy
            -- can be the one that creates the table, and a shape missing these columns makes the
            -- very next create (here or in the CLI) fail with `no such column`.
            hardware_profile TEXT, hardware_profile_version INTEGER,
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
    audit_write.audit(actor, action, target, details, conn=conn)


# ── JupyterHub named-server spawning (ADR 0090) ────────────────────────────────
# The dashboard's docker-socket-proxy forbids container creation, so a workbench is spawned as a
# JupyterHub *named server* (the Hub holds real Docker access + proxies HTTP/WS on 18888). When the
# Hub env is unset the router degrades to intent-only (DB status flip, no spawn), so tests + the
# no-Hub dev path keep working.


def _hub_cfg() -> dict:
    return {
        "api": os.getenv("JUPYTERHUB_API_URL", "").rstrip("/"),
        "public": os.getenv("JUPYTERHUB_PUBLIC_URL", "").rstrip("/"),
        "token": os.getenv("JUPYTERHUB_DASHBOARD_TOKEN", ""),
        "user": os.getenv("JUPYTERHUB_WORKBENCH_USER", "admin"),
    }


def _hub_enabled() -> bool:
    c = _hub_cfg()
    return bool(c["api"] and c["token"])


def _server_name(project: str, name: str) -> str:
    """URL-safe JupyterHub named-server id encoding project+workbench."""
    return re.sub(r"[^A-Za-z0-9-]", "-", f"{project}-{name}").strip("-")[:48]


def _open_url(project: str, name: str) -> str | None:
    c = _hub_cfg()
    if not c["public"]:
        return None
    return f"{c['public']}/user/{c['user']}/{_server_name(project, name)}/lab"


def _hub_request(method: str, path: str) -> int:
    """One Hub REST call (sync — run via to_thread). Returns the HTTP status (0 on transport error)."""
    c = _hub_cfg()
    req = urllib.request.Request(
        c["api"] + path,
        method=method,
        headers={"Authorization": "token " + c["token"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed internal Hub URL
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code  # 400 = already running/stopped, 409 = user exists — all non-fatal
    except Exception:
        return 0


def _hub_spawn(project: str, name: str) -> None:
    c = _hub_cfg()
    server = _server_name(project, name)
    _hub_request("POST", f"/users/{c['user']}")  # ensure user exists (409 if present)
    _hub_request("POST", f"/users/{c['user']}/servers/{server}")  # 201/202 spawn, 400 if running


def _hub_stop(project: str, name: str) -> None:
    c = _hub_cfg()
    _hub_request("DELETE", f"/users/{c['user']}/servers/{_server_name(project, name)}")


def _field(r: sqlite3.Row, name: str):
    """A column that may be absent on a table an older build created (ADR 0157 Phase 2)."""
    return r[name] if name in r.keys() else None


def _row_to_view(r: sqlite3.Row) -> dict:
    running = (r["status"] or "").upper() == "RUNNING"
    return {
        "name": r["name"],
        "project": r["project"],
        "image": r["image"],
        "cpu": r["cpu"],
        "memoryGb": r["memory_gb"],
        # Read-only: which named hardware profile + version this workbench was created from.
        "hardwareProfile": _field(r, "hardware_profile"),
        "hardwareProfileVersion": _field(r, "hardware_profile_version"),
        "volume": r["storage_volume"],
        "status": r["status"],
        "createdAt": r["created_at"],
        "createdBy": r["created_by"],
        # Open URL for a running workbench (None when the Hub isn't wired) — computed, not stored.
        "url": _open_url(r["project"], r["name"]) if (running and _hub_enabled()) else None,
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
async def create_workbench_view(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Create a project-bound workbench (notebook) — admin / project.manage; audited.

    Body: ``{project, name, image?, cpu?, memoryGb?, hardwareProfile?}``. ``hardwareProfile``
    (ADR 0157 Phase 2) names a profile applicable to ``workbench``/``any`` whose cpu/memory become
    the defaults — explicit ``cpu``/``memoryGb`` still win, exactly as on the CLI. Reuses ``examlops.workbenches.create_workbench``
    (the same code path as ``exa workbench create``) so the dashboard can never drift from the CLI.
    Registers a per-workbench storage volume and a ``kind='storage'`` project resource. Status STOPPED.
    """
    _require_manage(principal)
    project = (payload.get("project") or "").strip()
    name = (payload.get("name") or "").strip()
    image = (payload.get("image") or "").strip() or None
    hardware_profile = payload.get("hardwareProfile")
    if hardware_profile is not None and not isinstance(hardware_profile, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "hardwareProfile must be a string")
    hardware_profile = (hardware_profile or "").strip() or None
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
            hardware_profile=hardware_profile,
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
    details: dict = {"project": project, "image": image}
    if created.get("hardware_profile"):
        details["hardware_profile"] = created["hardware_profile"]
        details["hardware_profile_version"] = created.get("hardware_profile_version")
    _audit(conn, actor, "workbench_created", name, details)
    conn.commit()
    conn.close()
    return {
        "name": created["name"],
        "project": created["project"],
        "image": created["image"],
        "cpu": created["cpu"],
        "memoryGb": created["memory_gb"],
        "hardwareProfile": created.get("hardware_profile"),
        "hardwareProfileVersion": created.get("hardware_profile_version"),
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
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
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
            # Actually spawn the JupyterHub named server (off the event loop). Degrades to
            # intent-only when the Hub isn't wired, so the DB status still reflects RUNNING.
            if _hub_enabled():
                await asyncio.to_thread(_hub_spawn, project, name)
                result["url"] = _open_url(project, name)
        else:
            if not wb.stop_workbench(name, project):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"workbench '{name}' not found in project '{project}'",
                )
            if _hub_enabled():
                await asyncio.to_thread(_hub_stop, project, name)
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
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Delete a workbench definition (admin / project.manage; audited)."""
    _require_manage(principal)
    wb = _examlops_workbenches()
    if not wb.delete_workbench(name, project):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"workbench '{name}' not found in project '{project}'"
        )
    if _hub_enabled():  # tear down its named server too
        await asyncio.to_thread(_hub_stop, project, name)
    conn = _connect()
    _ensure_table(conn)
    _audit(conn, principal.get("sub", "?"), "workbench_deleted", name, {"project": project})
    conn.commit()
    conn.close()
    return {"project": project, "name": name, "deleted": True}

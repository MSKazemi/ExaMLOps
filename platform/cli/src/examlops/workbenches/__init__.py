"""P5 — Project Workbenches (ADR 0090).

On-demand, project-bound dev environments (the RHOAI *workbench* analogue). ExaMLOps records the
workbench intent + wiring in ``platform.db`` and injects the project's Named Connections (P2) as
environment variables; the actual spawn is delegated to the runtime (JupyterHub/Docker) behind the
``spawn``/``stop`` seam, consistent with ADR 0084's advisory boundary. Self-contained (own idempotent
table). A workbench is registered as a project resource (``kind='storage'``).
"""

from __future__ import annotations

import re
from typing import Any

from examlops.data import get_db
from examlops.data.projects import get_project

_DEFAULT_IMAGE = "jupyter/scipy-notebook:latest"


class WorkbenchError(Exception):
    """Raised for unknown workbench / unknown project."""


def _ensure_table() -> None:
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS workbenches (
                   name           TEXT NOT NULL,
                   project        TEXT NOT NULL,
                   image          TEXT NOT NULL DEFAULT 'jupyter/scipy-notebook:latest',
                   cpu            REAL,
                   memory_gb      REAL,
                   storage_volume TEXT,
                   status         TEXT NOT NULL DEFAULT 'STOPPED',  -- STOPPED | RUNNING
                   created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   created_by     TEXT,
                   PRIMARY KEY (project, name)
               )"""
        )


def _env_key(*parts: str) -> str:
    raw = "_".join(parts)
    return re.sub(r"[^A-Z0-9_]", "_", raw.upper())


def connection_env(
    project: str, *, tenant: str = "default", actor: str | None = None
) -> dict[str, str]:
    """Resolve the project's Named Connections into environment variables (RHOAI injection).

    For each connection ``c``: every non-secret config key becomes ``EXA_CONN_<C>_<KEY>`` and its
    secret (if any) becomes ``EXA_CONN_<C>_SECRET``. Never raises — a connection that fails to
    resolve its secret is skipped for that value.
    """
    from examlops import connections as _conn

    env: dict[str, str] = {}
    for c in _conn.list_connections(project=project):
        name = c["name"]
        for k, v in c["config"].items():
            env[_env_key("EXA_CONN", name, k)] = str(v)
        if c.get("secret_ref"):
            try:
                resolved = _conn.resolve_connection(
                    name, project=project, tenant=tenant, actor=actor
                )
                if "secret" in resolved:
                    env[_env_key("EXA_CONN", name, "SECRET")] = str(resolved["secret"])
            except Exception:
                pass
    return env


def create_workbench(
    name: str,
    project: str,
    *,
    image: str | None = None,
    cpu: float | None = None,
    memory_gb: float | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Define a workbench in a project (status STOPPED). Raises if the project is unknown."""
    if not get_project(project):
        raise WorkbenchError(f"project {project!r} not found")
    _ensure_table()
    volume = f"{project}-{name}-data"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO workbenches (name, project, image, cpu, memory_gb, storage_volume, created_by)
               VALUES (?,?,?,?,?,?,?)""",
            (name, project, image or _DEFAULT_IMAGE, cpu, memory_gb, volume, created_by),
        )
    from examlops.data.projects import assign_resource_to_project

    assign_resource_to_project(project, "storage", f"workbench:{name}", added_by=created_by)
    return get_workbench(name, project)  # type: ignore[return-value]


def get_workbench(name: str, project: str) -> dict[str, Any] | None:
    _ensure_table()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM workbenches WHERE project=? AND name=?", (project, name)
        ).fetchone()
    return dict(row) if row else None


def list_workbenches(project: str | None = None) -> list[dict[str, Any]]:
    _ensure_table()
    with get_db() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM workbenches ORDER BY project, name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workbenches WHERE project=? ORDER BY name", (project,)
            ).fetchall()
    return [dict(r) for r in rows]


def _set_status(name: str, project: str, status: str) -> bool:
    _ensure_table()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE workbenches SET status=? WHERE project=? AND name=?", (status, project, name)
        )
        return cur.rowcount > 0


def start_workbench(name: str, project: str, *, actor: str | None = None) -> dict[str, Any]:
    """Mark a workbench RUNNING and return its launch spec (image, mounted volume, injected env).

    The returned spec is what a runtime backend (JupyterHub/Docker) consumes to spawn the pod;
    ExaMLOps records intent + wiring and leaves enforcement to that runtime (ADR 0084 boundary).
    """
    wb = get_workbench(name, project)
    if not wb:
        raise WorkbenchError(f"workbench {name!r} not found in project {project!r}")
    if not _set_status(name, project, "RUNNING"):
        raise WorkbenchError("failed to update status")
    return {
        "name": name,
        "project": project,
        "image": wb["image"],
        "volume": wb["storage_volume"],
        "env": connection_env(project, actor=actor),
        "status": "RUNNING",
    }


def stop_workbench(name: str, project: str) -> bool:
    """Mark a workbench STOPPED. Returns True if it existed."""
    return _set_status(name, project, "STOPPED")


def delete_workbench(name: str, project: str) -> bool:
    """Delete a workbench definition. Returns True if removed."""
    _ensure_table()
    with get_db() as conn:
        cur = conn.execute("DELETE FROM workbenches WHERE project=? AND name=?", (project, name))
        removed = cur.rowcount > 0
    if removed:
        from examlops.data.projects import remove_project_resource

        remove_project_resource(project, "storage", f"workbench:{name}")
    return removed

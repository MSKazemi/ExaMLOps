"""Projects (Unified Project Workspace, ADR 0086) — reads/writes the shared platform.db.

Surfaces the ``exa project`` primitive in the dashboard: a Project groups models, pipelines,
serving, connections, and people (owner/editor/viewer via the D6 ``authz_relations`` table). Reads
are viewer-gated; mutations require the ``project.manage`` capability and are audited. The console is
gated by the ``projectsConsole`` feature flag on the frontend.
"""

from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from capabilities import PROJECT_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/v1/projects", tags=["projects"])
_viewer = require_role("viewer")
_admin = require_role("admin")

_RESOURCE_KINDS = {"model", "pipeline", "serving_endpoint", "connection", "dataset", "storage"}


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _connect() -> sqlite3.Connection:
    conn = connect(_db_path())
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Idempotently declare the tables this router reads (matches platform_db init)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY, description TEXT,
            cpu_limit REAL NOT NULL DEFAULT 4.0, memory_limit_gb REAL NOT NULL DEFAULT 8.0,
            storage_gb REAL NOT NULL DEFAULT 50.0, gpu_limit INTEGER NOT NULL DEFAULT 0,
            network_name TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by TEXT, updated_at DATETIME
        );
        CREATE TABLE IF NOT EXISTS project_models (
            project TEXT NOT NULL, model TEXT NOT NULL,
            assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (project, model)
        );
        CREATE TABLE IF NOT EXISTS project_resources (
            project TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
            added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, added_by TEXT,
            PRIMARY KEY (project, kind, ref)
        );
        CREATE TABLE IF NOT EXISTS namespace_models (
            model TEXT NOT NULL, namespace TEXT NOT NULL DEFAULT 'default',
            assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (model, namespace)
        );
        CREATE TABLE IF NOT EXISTS project_budgets (
            project TEXT PRIMARY KEY, gpu_hours_budget REAL, cost_budget REAL,
            period TEXT NOT NULL DEFAULT 'monthly', updated_at DATETIME, updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS model_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_name TEXT, version INTEGER,
            run_id TEXT, job_id TEXT, gpu_hours REAL, cost_usd REAL, recorded_at TEXT, project TEXT
        );
        CREATE TABLE IF NOT EXISTS authz_relations (
            subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL,
            actor TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
        );
    """)


def _project_models(conn: sqlite3.Connection, project: str) -> list[str]:
    rows = conn.execute(
        """SELECT ref AS model FROM project_resources WHERE project=? AND kind='model'
           UNION SELECT model FROM project_models WHERE project=?
           ORDER BY model""",
        (project, project),
    ).fetchall()
    return [r["model"] for r in rows]


def _consumption(conn: sqlite3.Connection, project: str) -> dict:
    row = conn.execute(
        """WITH members(model) AS (
               SELECT ref FROM project_resources WHERE project=? AND kind='model'
               UNION SELECT model FROM project_models WHERE project=?
               UNION SELECT model FROM namespace_models WHERE namespace=?
           )
           SELECT COALESCE(SUM(c.gpu_hours),0) AS gpu_hours, COALESCE(SUM(c.cost_usd),0) AS cost_usd
           FROM members m JOIN model_costs c ON c.model_name = m.model""",
        (project, project, project),
    ).fetchone()
    return {"gpu_hours": float(row["gpu_hours"]), "cost_usd": float(row["cost_usd"])}


def _members(conn: sqlite3.Connection, project: str) -> list[dict]:
    rows = conn.execute(
        "SELECT subject, relation, actor, created_at FROM authz_relations "
        "WHERE object=? ORDER BY subject",
        (f"project:{project}",),
    ).fetchall()
    return [
        {
            "subject": r["subject"],
            "role": r["relation"],
            "grantedBy": r["actor"],
            "when": r["created_at"],
        }
        for r in rows
    ]


def _audit(conn: sqlite3.Connection, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


@router.get("")
async def list_projects_view(_=Depends(_viewer)) -> list[dict]:
    """All projects with quota + resource/member counts (viewer)."""
    try:
        conn = _connect()
        _ensure_tables(conn)
        rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        out = []
        for r in rows:
            models = _project_models(conn, r["name"])
            res_count = conn.execute(
                "SELECT COUNT(*) AS n FROM project_resources WHERE project=?", (r["name"],)
            ).fetchone()["n"]
            member_count = conn.execute(
                "SELECT COUNT(*) AS n FROM authz_relations WHERE object=?",
                (f"project:{r['name']}",),
            ).fetchone()["n"]
            out.append(
                {
                    "name": r["name"],
                    "description": r["description"],
                    "status": r["status"],
                    "quota": {
                        "cpuLimit": r["cpu_limit"],
                        "memoryLimitGb": r["memory_limit_gb"],
                        "storageGb": r["storage_gb"],
                        "gpuLimit": r["gpu_limit"],
                    },
                    "modelCount": len(models),
                    "resourceCount": max(res_count, len(models)),
                    "memberCount": member_count,
                }
            )
        conn.close()
        return out
    except Exception:
        return []


@router.get("/{name}")
async def project_anatomy(name: str, _=Depends(_viewer)) -> dict:
    """Full anatomy: quota, resources by kind, members, budget, consumption (viewer)."""
    conn = _connect()
    _ensure_tables(conn)
    p = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    res_rows = conn.execute(
        "SELECT kind, ref FROM project_resources WHERE project=? ORDER BY kind, ref", (name,)
    ).fetchall()
    resources: dict[str, list[str]] = {}
    for rr in res_rows:
        resources.setdefault(rr["kind"], []).append(rr["ref"])
    models = _project_models(conn, name)
    if models:
        resources["model"] = models
    budget_row = conn.execute("SELECT * FROM project_budgets WHERE project=?", (name,)).fetchone()
    budget = _budget_view(budget_row)
    anatomy = {
        "name": p["name"],
        "description": p["description"],
        "status": p["status"],
        "quota": {
            "cpuLimit": p["cpu_limit"],
            "memoryLimitGb": p["memory_limit_gb"],
            "storageGb": p["storage_gb"],
            "gpuLimit": p["gpu_limit"],
        },
        "resources": resources,
        "members": _members(conn, name),
        "budget": budget,
        "consumption": _consumption(conn, name),
        "createdAt": p["created_at"],
        "createdBy": p["created_by"],
        # Project Anatomy P8 (ADR 0093): storage · connections · pipelines. Each is fail-open
        # (a missing table/row yields None/[]) and secret-safe (connections expose hasSecret only).
        "storage": _storage(conn, name),
        "connections": _connections(conn, name),
        "pipelines": _pipelines(conn, name, models),
    }
    conn.close()
    return anatomy


def _budget_view(row: sqlite3.Row | None) -> dict | None:
    """Shape a ``project_budgets`` row for the UI.

    The canonical ``platform_db`` schema names the columns ``gpu_hours_budget`` / ``cost_budget``
    (written by ``examlops.data.projects.set_project_budget`` and ``exa``). Older/divergent DBs may
    have ``gpu_hours`` / ``cost_usd``. Read defensively so a budget set via the CLI renders here
    instead of 500-ing the whole anatomy endpoint on a missing column.
    """
    if row is None:
        return None
    keys = row.keys()
    gpu = (
        row["gpu_hours_budget"]
        if "gpu_hours_budget" in keys
        else (row["gpu_hours"] if "gpu_hours" in keys else None)
    )
    cost = (
        row["cost_budget"]
        if "cost_budget" in keys
        else (row["cost_usd"] if "cost_usd" in keys else None)
    )
    return {"gpuHours": gpu, "costUsd": cost}


def _storage(conn, name: str) -> dict | None:
    try:
        r = conn.execute("SELECT * FROM project_storage WHERE project=?", (name,)).fetchone()
        if not r:
            return None
        return {
            "bucket": r["bucket"],
            "prefix": r["prefix"],
            "quotaGb": r["quota_gb"],
            "usedBytes": r["used_bytes"],
            "connectionRef": r["connection_ref"],
        }
    except Exception:
        return None


def _connections(conn, name: str) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT name, kind, secret_ref FROM connections WHERE project=? ORDER BY name", (name,)
        ).fetchall()
        return [
            {"name": r["name"], "kind": r["kind"], "hasSecret": bool(r["secret_ref"])} for r in rows
        ]
    except Exception:
        return []


def _pipelines(conn, name: str, models: list[str]) -> dict:
    try:
        reg = {
            r["kind"]: r
            for r in conn.execute(
                "SELECT * FROM project_pipelines WHERE project=?", (name,)
            ).fetchall()
        }
    except Exception:
        reg = {}
    prefect = None
    if models or reg.get("prefect"):
        pr = reg.get("prefect")
        prefect = {
            "deployments": [f"examlops-{m.lower()}" for m in models],
            "schedule": pr["schedule"] if pr else None,
            "lastRunAt": pr["last_run_at"] if pr else None,
            "status": pr["status"] if pr else "unknown",
        }
    rayserve = None
    if models or reg.get("rayserve"):
        traffic: dict = {}
        for m in models:
            try:
                tr = conn.execute("SELECT rules FROM traffic_rules WHERE model=?", (m,)).fetchone()
                if tr:
                    traffic[m] = json.loads(tr["rules"])
            except Exception:
                pass
        rr = reg.get("rayserve")
        rayserve = {
            "models": models,
            "traffic": traffic,
            "status": rr["status"] if rr else "unknown",
        }
    return {"prefect": prefect, "rayserve": rayserve}


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, PROJECT_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, PROJECT_MANAGE))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project_view(payload: dict = Body(...), principal: dict = Depends(_admin)) -> dict:
    """Create a project (admin / project.manage; audited)."""
    _require_manage(principal)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    conn = _connect()
    _ensure_tables(conn)
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Project '{name}' already exists")
    conn.execute(
        """INSERT INTO projects (name, description, cpu_limit, memory_limit_gb, storage_gb,
           gpu_limit, network_name, created_by) VALUES (?,?,?,?,?,?,?,?)""",
        (
            name,
            payload.get("description"),
            float(payload.get("cpuLimit", 4.0)),
            float(payload.get("memoryLimitGb", 8.0)),
            float(payload.get("storageGb", 50.0)),
            int(payload.get("gpuLimit", 0)),
            f"examlops-{name}",
            principal.get("sub"),
        ),
    )
    _audit(conn, principal.get("sub", "?"), "project_created", name, {"via": "dashboard"})
    conn.commit()
    conn.close()
    return {"name": name, "status": "ACTIVE"}


@router.post("/{name}/resources")
async def assign_resource_view(
    name: str, payload: dict = Body(...), principal: dict = Depends(_admin)
) -> dict:
    """Attach a resource (kind/ref) to a project (admin / project.manage; audited)."""
    _require_manage(principal)
    kind = payload.get("kind", "model")
    ref = (payload.get("ref") or "").strip()
    if kind not in _RESOURCE_KINDS or not ref:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "valid kind and ref required")
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn.execute(
        "INSERT OR REPLACE INTO project_resources (project, kind, ref, added_by) VALUES (?,?,?,?)",
        (name, kind, ref, principal.get("sub")),
    )
    if kind == "model":
        conn.execute(
            "INSERT OR REPLACE INTO project_models (project, model) VALUES (?,?)", (name, ref)
        )
    _audit(
        conn,
        principal.get("sub", "?"),
        "project_resource_assigned",
        ref,
        {"project": name, "kind": kind},
    )
    conn.commit()
    conn.close()
    return {"project": name, "kind": kind, "ref": ref}


@router.post("/{name}/members")
async def add_member_view(
    name: str, payload: dict = Body(...), principal: dict = Depends(_admin)
) -> dict:
    """Add a person to a project with owner/editor/viewer role (admin / project.manage; audited)."""
    _require_manage(principal)
    subject = (payload.get("subject") or "").strip()
    role = payload.get("role", "viewer")
    if not subject or role not in {"owner", "editor", "viewer"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject and valid role required")
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn.execute(
        "INSERT INTO authz_relations (subject, relation, object, actor) VALUES (?,?,?,?)",
        (subject, role, f"project:{name}", principal.get("sub")),
    )
    _audit(
        conn,
        principal.get("sub", "?"),
        "project_member_added",
        subject,
        {"project": name, "role": role},
    )
    conn.commit()
    conn.close()
    return {"project": name, "subject": subject, "role": role}


@router.delete("/{name}/members/{subject}")
async def remove_member_view(name: str, subject: str, principal: dict = Depends(_admin)) -> dict:
    """Remove a person from a project (admin / project.manage; audited)."""
    _require_manage(principal)
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    cur = conn.execute(
        "DELETE FROM authz_relations WHERE subject=? AND object=?", (subject, f"project:{name}")
    )
    removed = cur.rowcount
    _audit(
        conn,
        principal.get("sub", "?"),
        "project_member_removed",
        subject,
        {"project": name, "removed": removed},
    )
    conn.commit()
    conn.close()
    if removed == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"'{subject}' is not a member of project '{name}'"
        )
    return {"project": name, "subject": subject, "removed": removed}


@router.post("/{name}/storage")
async def bind_storage_view(
    name: str, payload: dict = Body(default={}), principal: dict = Depends(_admin)
) -> dict:
    """Ensure per-project storage and (optionally) bind a connection to it (P6, ADR 0091).

    Body: ``{connectionRef?}``. Calls the shared ``examlops.platform_db`` helpers so the storage
    layout matches ``exa project storage`` exactly. Admin / project.manage; audited.
    """
    _require_manage(principal)
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn.close()
    try:
        from examlops import platform_db as _pdb  # lazy, guarded (503 if unavailable)
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage binding requires the examlops package (not available in this deployment)",
        ) from exc
    actor = principal.get("sub", "?")
    storage = _pdb.ensure_project_storage(name)
    if storage is None:  # pragma: no cover - ensure returns None only on backend failure
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "could not provision storage")
    connection_ref = (payload.get("connectionRef") or "").strip() or None
    bound = False
    if connection_ref:
        bound = _pdb.bind_project_connection(name, connection_ref, actor=actor)
        storage = _pdb.ensure_project_storage(name)  # re-read to reflect the binding
    conn = _connect()
    _audit(
        conn,
        actor,
        "project_storage_bound",
        name,
        {"connectionRef": connection_ref, "bound": bound},
    )
    conn.commit()
    conn.close()
    return {
        "project": name,
        "bucket": storage.get("bucket"),
        "prefix": storage.get("prefix"),
        "connectionRef": storage.get("connection_ref"),
        "bound": bound,
    }


@router.delete("/{name}")
async def delete_project_view(name: str, principal: dict = Depends(_admin)) -> dict:
    """Delete a project and its membership/resource rows (admin / project.manage; audited).

    Removes the project row plus its ``project_models``/``project_resources``/``authz_relations``
    (membership) and best-effort ``project_budgets``/``project_storage``/``project_pipelines``.
    Does not delete the underlying models or connections themselves — only the project grouping.
    """
    _require_manage(principal)
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn.execute("DELETE FROM project_models WHERE project=?", (name,))
    conn.execute("DELETE FROM project_resources WHERE project=?", (name,))
    conn.execute("DELETE FROM authz_relations WHERE object=?", (f"project:{name}",))
    for tbl in ("project_budgets", "project_storage", "project_pipelines"):
        try:
            conn.execute(f"DELETE FROM {tbl} WHERE project=?", (name,))
        except sqlite3.OperationalError:
            pass  # optional table not present in this DB
    conn.execute("DELETE FROM projects WHERE name=?", (name,))
    _audit(conn, principal.get("sub", "?"), "project_deleted", name, {"via": "dashboard"})
    conn.commit()
    conn.close()
    return {"name": name, "deleted": True}

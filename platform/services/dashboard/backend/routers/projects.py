"""Projects (Unified Project Workspace, ADR 0086) — reads/writes the shared platform.db.

Surfaces the ``exa project`` primitive in the dashboard: a Project groups models, pipelines,
serving, connections, and people (owner/editor/viewer via the D6 ``authz_relations`` table). Reads
are viewer-gated; mutations require the ``project.manage`` capability and are audited. The console is
gated by the ``projectsConsole`` feature flag on the frontend.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

import audit_write
from auth import require_role
from bff import aggregate
from capabilities import PROJECT_MANAGE, can, deny_reason, require_capability
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, status

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["projects"])
_viewer = require_role("viewer")
_admin = require_role("admin")

_RESOURCE_KINDS = {"model", "pipeline", "serving_endpoint", "connection", "dataset", "storage"}


_REL_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def _authz_subject(principal: dict) -> str:
    """Who the relationship store knows this session as (ADR 0014 decision 4/5).

    A federated principal (``idp`` claim) is its own subject. A shared-password session has no
    per-user identity, so it maps to the stable ``legacy:<role>`` subject, which holds the
    migration grants (owner/editor/viewer on project ``default``) and nothing else.
    """
    if principal.get("idp"):
        return str(principal.get("sub") or "?")
    return f"legacy:{principal.get('role', 'viewer')}"


def _project_allowed(principal: dict, relation: str, name: str) -> bool:
    try:
        from examlops.authz import guard as _g  # type: ignore
    except ImportError:  # pragma: no cover - examlops absent: tenancy cannot be on
        return True
    projects = principal.get("projects") or {}  # project roles asserted by the IdP (ADR 0120)
    if _REL_RANK.get(str(projects.get(name, "")), 0) >= _REL_RANK.get(relation, 99):
        from examlops import authz  # type: ignore

        if authz.multitenancy_enabled():
            return True
    return _g.allowed(_authz_subject(principal), relation, name)


def _project_guard(principal: dict, relation: str, name: str) -> None:
    """403 unless the session holds ``relation`` on project ``name``. No-op with tenancy off."""
    if not _project_allowed(principal, relation, name):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"'{_authz_subject(principal)}' lacks '{relation}' on project '{name}'",
        )


def _resource_guard(principal: dict, relation: str, kind: str, ref: str) -> None:
    """403 unless the session holds ``relation`` wherever ``kind``/``ref`` lives now (ADR 0014 d4).

    Project membership is the authorization key for models and datasets, so attaching one to a
    project changes who controls it; editing the target project alone must not be enough. 503 when
    the membership store cannot be read (fail closed). No-op with tenancy off.
    """
    try:
        from examlops.authz import guard as _g  # type: ignore
    except ImportError:  # pragma: no cover - examlops absent: tenancy cannot be on
        return
    if kind not in _g.RESOURCE_KINDS:
        return
    subject = _authz_subject(principal)
    try:
        ok = _g.resource_allowed(
            subject, relation, kind, ref, asserted_projects=principal.get("projects") or None
        )
    except _g.ModelScopeUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Project membership is unavailable; refusing rather than guessing",
        ) from exc
    if not ok:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"'{subject}' lacks '{relation}' on {kind} '{ref}' where it lives now",
        )


def _fga_sync(kind: str, subject: str, relation: str, obj: str) -> None:
    """Mirror a membership change into OpenFGA when it is configured (else a no-op)."""
    try:
        from examlops.authz import openfga_client as _f  # type: ignore
    except ImportError:  # pragma: no cover
        return
    cfg = _f.config_from_env()
    if cfg is None:
        return
    try:
        (_f.write_grant if kind == "grant" else _f.delete_grant)(cfg, subject, relation, obj)
    except _f.OpenFgaError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"OpenFGA refused: {exc}") from exc


def _db_path() -> str:
    return platform_db_path()


def _connect() -> sqlite3.Connection:
    conn = connect(_db_path())
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Idempotently declare the tables this router reads.

    Prefers the product's own schema: when ``examlops`` is importable, ``platform_db.init_db()``
    is the single definition and this function adds nothing of its own. The inline DDL below is
    only the degraded path for a deployment without the package (the same condition the write
    endpoints answer with a 503), and it is kept column-for-column and constraint-for-constraint
    identical to ``platform_db`` — an earlier version was not, and a table it created without
    ``UNIQUE (subject, relation, object)`` made ``exa project add-member`` fail outright against
    a database the dashboard had initialised first.
    """
    try:
        from examlops import platform_db as _pdb  # type: ignore

        _pdb.init_db()
        return
    except ImportError:  # pragma: no cover - only when examlops is not installed
        pass
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
            period TEXT NOT NULL DEFAULT 'monthly',
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS model_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_name TEXT NOT NULL, version INTEGER NOT NULL,
            run_id TEXT, job_id TEXT, gpu_hours REAL, cost_usd REAL,
            recorded_at TEXT NOT NULL, project TEXT
        );
        CREATE TABLE IF NOT EXISTS authz_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL,
            actor TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (subject, relation, object)
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
    audit_write.audit(actor, action, target, details, conn=conn)


@router.get("")
async def list_projects_view(principal: dict = Depends(_viewer)) -> list[dict]:
    """All projects with quota + resource/member counts (viewer)."""
    try:
        conn = _connect()
        _ensure_tables(conn)
        rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        out = []
        for r in rows:
            if not _project_allowed(principal, "viewer", r["name"]):
                continue  # multi-tenant: a session sees only the projects it may read
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


@router.get("/zoo-models")
async def zoo_models_view(_=Depends(_viewer)) -> dict:
    """List the Model-Zoo/pack models that can be onboarded (candidates for a per-model project).

    Registered before ``/{name}`` so the literal path is not captured by the anatomy route.
    """
    adopt = _examlops_adopt()
    return {
        "models": [{"model": m, "project": adopt.project_name_for(m)} for m in adopt.zoo_models()]
    }


@router.get("/{name}")
async def project_anatomy(name: str, principal: dict = Depends(_viewer)) -> dict:
    """Full anatomy: quota, resources by kind, members, budget, consumption (viewer).

    ADR 0093 decision 3: assembled through ``bff.aggregate`` — every section is its own source
    with its own timeout, so one slow or broken section (a Prefect that does not answer, a
    missing ``connections`` table) degrades to its empty shape and is named under ``_partial``
    instead of failing the page. Only the project row itself is required: no row ⇒ 404.
    """
    _project_guard(principal, "viewer", name)
    conn = _connect()
    try:
        _ensure_tables(conn)
        p = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")

    def _resources(c: sqlite3.Connection) -> dict[str, list[str]]:
        resources: dict[str, list[str]] = {}
        for rr in c.execute(
            "SELECT kind, ref FROM project_resources WHERE project=? ORDER BY kind, ref", (name,)
        ).fetchall():
            resources.setdefault(rr["kind"], []).append(rr["ref"])
        models = _project_models(c, name)
        if models:
            resources["model"] = models
        return resources

    def _budget(c: sqlite3.Connection) -> dict | None:
        return _budget_view(
            c.execute("SELECT * FROM project_budgets WHERE project=?", (name,)).fetchone()
        )

    parts = await aggregate(
        {
            "resources": _on_conn(_resources),
            "members": _on_conn(lambda c: _members(c, name)),
            "budget": _on_conn(_budget),
            "consumption": _on_conn(lambda c: _consumption(c, name)),
            # Project Anatomy P8 (ADR 0093): storage · connections · pipelines, secret-safe
            # (connections expose hasSecret only). Pipelines are hydrated live (ADR 0092).
            "storage": _on_conn(lambda c: _storage(c, name)),
            "connections": _on_conn(lambda c: _connections(c, name)),
            "pipelines": lambda: _pipelines_live(name),
        },
        timeout=_ANATOMY_TIMEOUT,
    )
    partial: list[str] = list(parts.get("_partial", []))
    if "pipelines" in partial:
        # The live overlay timed out or broke: the registry view needs no network, so the panel
        # still renders its last observed state instead of disappearing.
        try:
            parts["pipelines"] = _registry_pipelines_view(name)
        except Exception as exc:  # noqa: BLE001 - a partial view beats a 500
            log.warning("project %s: registry pipelines view unavailable: %s", name, exc)
            parts["pipelines"] = {"prefect": None, "rayserve": None}
    if partial:
        log.warning("project anatomy for %s is partial: %s", name, ", ".join(partial))

    anatomy: dict[str, Any] = {
        "name": p["name"],
        "description": p["description"],
        "status": p["status"],
        # Isolated namespace / network the project's resources bind into (ADR 0084/0086).
        "namespace": p["network_name"],
        "quota": {
            "cpuLimit": p["cpu_limit"],
            "memoryLimitGb": p["memory_limit_gb"],
            "storageGb": p["storage_gb"],
            "gpuLimit": p["gpu_limit"],
        },
        "resources": parts.get("resources", {}),
        "members": parts.get("members", []),
        "budget": parts.get("budget"),
        # An unavailable consumption renders as zero ONLY alongside `_partial: ["consumption"]`,
        # which the page turns into a visible "some sections could not be loaded" notice.
        "consumption": parts.get("consumption", {"gpu_hours": 0.0, "cost_usd": 0.0}),
        "createdAt": p["created_at"],
        "createdBy": p["created_by"],
        "storage": parts.get("storage"),
        "connections": parts.get("connections", []),
        "pipelines": parts.get("pipelines", {"prefect": None, "rayserve": None}),
    }
    if partial:
        anatomy["_partial"] = sorted(partial)
    return anatomy


#: Per-section budget for the anatomy fan-out. The live pipeline read is the slow one: it is
#: bounded by its own per-request timeout (EXAMLOPS_PROJECT_PIPELINES_TIMEOUT); this caps the sum.
_ANATOMY_TIMEOUT = 8.0


def _on_conn(fn):
    """A zero-arg ``bff`` source that runs ``fn`` on its own connection (sources run in threads)."""

    def run():
        c = _connect()
        try:
            return fn(c)
        finally:
            c.close()

    return run


def _live_pipeline_sources():
    """The Prefect / Ray Serve URLs this deployment explicitly names (Compose sets both).

    Never the settings' ``localhost`` defaults: a dashboard nobody pointed at Prefect must not
    report whatever happens to listen on its host. ``EXAMLOPS_PROJECT_PIPELINES_LIVE=0`` disables.
    """
    from examlops import project_pipelines as _pp  # type: ignore

    return _pp.sources(
        prefect_url=os.getenv("PREFECT_URL") or os.getenv("PREFECT_API_URL") or None,
        serve_url=os.getenv("RAY_SERVE_URL") or None,
        serving_token=os.getenv("EXAMLOPS_SERVING_TOKEN", ""),
    )


def _pipelines_live(name: str) -> dict:
    """Both pipeline surfaces through the SAME code path as ``exa project pipelines`` (ADR 0092).

    Falls back to the router's own registry read only when the ``examlops`` package is absent.
    """
    try:
        from examlops.data.projects import get_project_pipelines  # type: ignore
    except ImportError:  # pragma: no cover - only when examlops is not installed
        return _registry_pipelines_view(name)
    return _pipelines_view(get_project_pipelines(name, live=_live_pipeline_sources()))


def _registry_pipelines_view(name: str) -> dict:
    """The network-free registry view (the floor the live overlay falls back to)."""
    return _on_conn(lambda c: _pipelines(c, name, _project_models(c, name)))()


def _pipelines_view(raw: dict) -> dict:
    """Shape the library's snake_case surfaces for the UI (camelCase, nothing secret)."""
    pf, ry = raw.get("prefect"), raw.get("rayserve")
    prefect = None
    if pf:
        prefect = {
            "deployments": list(pf.get("deployments") or []),
            "schedule": pf.get("schedule"),
            "lastRunAt": pf.get("last_run_at"),
            "lastRunState": pf.get("last_run_state"),
            "lastRunDeployment": pf.get("last_run_deployment"),
            "workPool": pf.get("work_pool"),
            "storagePrefix": pf.get("storage_prefix"),
            "status": pf.get("status") or "unknown",
            "source": pf.get("source") or "registry",
            "liveError": pf.get("live_error"),
        }
    rayserve = None
    if ry:
        rayserve = {
            "models": list(ry.get("models") or []),
            "traffic": ry.get("traffic") or {},
            "served": list(ry.get("served") or []),
            "unserved": list(ry.get("unserved") or []),
            "aliases": ry.get("aliases") or {},
            "health": ry.get("health") or {},
            "status": ry.get("status") or "unknown",
            "source": ry.get("source") or "registry",
            "liveError": ry.get("live_error"),
        }
    return {"prefect": prefect, "rayserve": rayserve}


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
                tr = conn.execute(
                    "SELECT rules FROM traffic_rules WHERE lower(model)=lower(?) "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (m,),
                ).fetchone()
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


def _examlops_projects():
    """Lazy, guarded import of the shared platform_db code path (503 if unavailable)."""
    try:
        from examlops import platform_db as _pdb  # type: ignore

        return _pdb
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "project edits require the examlops package (not available in this deployment)",
        ) from exc


def _grant_relation(subject: str, relation: str, obj: str, *, actor: str | None) -> None:
    """Grant a relation through the SAME code path as ``exa project add-member``.

    Not a raw INSERT: ``governance.grant_relation`` is idempotent via
    ``ON CONFLICT (subject, relation, object) DO NOTHING``, so re-adding a member is a no-op
    here exactly as it is on the CLI. A raw INSERT both duplicated rows and, against a
    database carrying the old constraint-less table, diverged from the CLI's behaviour.
    """
    try:
        from examlops.data import governance as _gov  # type: ignore
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "member changes require the examlops package (not available in this deployment)",
        ) from exc
    _gov.grant_relation(subject, relation, obj, actor=actor)


def _examlops_adopt():
    """Lazy, guarded import of the model-zoo onboarding code path (503 if unavailable).

    This is the SAME code path as ``exa modelzoo adopt`` / ``examlops.sdk.onboard_model`` — the
    dashboard never re-implements provisioning, so the three surfaces can never drift (Phase 42).
    """
    try:
        from examlops import modelzoo_adopt as _adopt  # type: ignore

        return _adopt
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "model onboarding requires the examlops package (not available in this deployment)",
        ) from exc


@router.put("/{name}")
async def update_project_view(
    name: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Edit a project's quota, budget, description and namespace (admin / project.manage; audited).

    Body (all optional): ``{description, cpuLimit, memoryLimitGb, storageGb, gpuLimit,
    networkName, gpuHoursBudget, costBudget}``. Quota + namespace route through
    ``examlops.platform_db.update_project_quota``; budget through ``set_project_budget`` — the same
    code paths as ``exa project`` so the dashboard can never drift from the CLI.
    """
    _require_manage(principal)
    _project_guard(principal, "editor", name)
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn.close()
    pdb = _examlops_projects()
    actor = principal.get("sub", "?")

    def _num(key: str, cast):
        v = payload.get(key)
        if v is None or v == "":
            return None
        try:
            return cast(v)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be numeric") from exc

    quota_changed = pdb.update_project_quota(
        name,
        cpu_limit=_num("cpuLimit", float),
        memory_limit_gb=_num("memoryLimitGb", float),
        storage_gb=_num("storageGb", float),
        gpu_limit=_num("gpuLimit", int),
        description=payload.get("description"),
        network_name=payload.get("networkName"),
    )

    budget_changed = False
    if "gpuHoursBudget" in payload or "costBudget" in payload:
        pdb.set_project_budget(
            name,
            _num("gpuHoursBudget", float),
            _num("costBudget", float),
            updated_by=actor,
        )
        budget_changed = True

    conn = _connect()
    _audit(
        conn,
        actor,
        "project_updated",
        name,
        {"via": "dashboard", "quota": quota_changed, "budget": budget_changed},
    )
    conn.commit()
    conn.close()
    return {"name": name, "quotaUpdated": quota_changed, "budgetUpdated": budget_changed}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project_view(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Create a project (admin / project.manage; audited)."""
    _require_manage(principal)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")

    def _num(key: str, default: float, cast=float):
        try:
            return cast(payload.get(key, default))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be a number") from exc

    cpu = _num("cpuLimit", 4.0)
    mem = _num("memoryLimitGb", 8.0)
    stor = _num("storageGb", 50.0)
    gpu = _num("gpuLimit", 0, int)
    # Shared code path (Phase 42): creation goes through examlops.data.projects.create_project —
    # the same helper `exa project create` uses — never a hand-rolled INSERT that can drift.
    _pdb = _examlops_projects()
    conn = _connect()
    _ensure_tables(conn)
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Project '{name}' already exists")
    _pdb.create_project(
        name,
        description=payload.get("description"),
        cpu_limit=cpu,
        memory_limit_gb=mem,
        storage_gb=stor,
        gpu_limit=gpu,
        created_by=principal.get("sub"),
    )
    from examlops.authz.guard import register_creator  # type: ignore

    register_creator(_authz_subject(principal), name)  # multi-tenant: the creator owns it
    _audit(conn, principal.get("sub", "?"), "project_created", name, {"via": "dashboard"})
    conn.commit()
    conn.close()
    return {"name": name, "status": "ACTIVE"}


@router.post("/{name}/resources")
async def assign_resource_view(
    name: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Attach a resource (kind/ref) to a project (admin / project.manage; audited)."""
    _require_manage(principal)
    _project_guard(principal, "editor", name)
    kind = payload.get("kind", "model")
    ref = (payload.get("ref") or "").strip()
    if kind not in _RESOURCE_KINDS or not ref:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "valid kind and ref required")
    _resource_guard(principal, "editor", kind, ref)
    # Shared code path (Phase 42): the helper validates kind, mirrors model-kind rows into
    # project_models, and is what `exa project assign` runs — the two surfaces cannot diverge.
    _pdb = _examlops_projects()
    if not _pdb.assign_resource_to_project(name, kind, ref, added_by=principal.get("sub")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn = _connect()
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
    name: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Add a person to a project with owner/editor/viewer role (admin / project.manage; audited)."""
    _require_manage(principal)
    _project_guard(principal, "owner", name)
    subject = (payload.get("subject") or "").strip()
    role = payload.get("role", "viewer")
    if not subject or role not in {"owner", "editor", "viewer"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject and valid role required")
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    _fga_sync("grant", subject, role, f"project:{name}")
    _grant_relation(subject, role, f"project:{name}", actor=principal.get("sub"))
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
async def remove_member_view(
    name: str,
    subject: str,
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Remove a person from a project (admin / project.manage; audited)."""
    _require_manage(principal)
    _project_guard(principal, "owner", name)
    conn = _connect()
    _ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        conn.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    for held in conn.execute(
        "SELECT relation FROM authz_relations WHERE subject=? AND object=?",
        (subject, f"project:{name}"),
    ).fetchall():
        _fga_sync("revoke", subject, held["relation"], f"project:{name}")
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
    name: str,
    payload: dict = Body(default={}),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Ensure per-project storage and (optionally) bind a connection to it (P6, ADR 0091).

    Body: ``{connectionRef?}``. Calls the shared ``examlops.platform_db`` helpers so the storage
    layout matches ``exa project storage`` exactly. Admin / project.manage; audited.
    """
    _require_manage(principal)
    _project_guard(principal, "editor", name)
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
        # Re-read to reflect the binding; a backend failure here must not 500 a bind that
        # already succeeded, so fall back to the pre-binding row rather than crash on None.
        storage = _pdb.ensure_project_storage(name) or storage
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
async def delete_project_view(
    name: str,
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Delete a project and its membership/resource rows (admin / project.manage; audited).

    Removes the project row plus its ``project_models``/``project_resources``/``authz_relations``
    (membership) and best-effort ``project_budgets``/``project_storage``/``project_pipelines``.
    Does not delete the underlying models or connections themselves — only the project grouping.
    """
    _require_manage(principal)
    _project_guard(principal, "owner", name)
    # Shared code path (Phase 42): the full cascade — membership, authz grants, budget/storage/
    # pipeline rows — lives in examlops.data.projects.delete_project, the same helper the CLI
    # uses, so the two surfaces can never disagree on a security-relevant cascade again.
    _pdb = _examlops_projects()
    if not _pdb.delete_project(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project '{name}' not found")
    conn = _connect()
    _audit(conn, principal.get("sub", "?"), "project_deleted", name, {"via": "dashboard"})
    conn.commit()
    conn.close()
    return {"name": name, "deleted": True}


# ── Model-zoo onboarding (one project per model, CLI/SDK/Dashboard share examlops.modelzoo_adopt) ──
@router.post("/onboard/{model}")
async def onboard_model_view(
    model: str,
    payload: dict = Body(default={}),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Provision one project for a Zoo model — project · storage · MinIO connection · budget · model ·
    workbench · pipeline surfaces (admin / project.manage; audited). Idempotent.

    Body (optional): ``{dryRun: bool, connectionName: str, provisionConnection: bool}``. Runs the
    SAME ``examlops.modelzoo_adopt.adopt_model`` code path as the CLI/SDK; the MinIO secret is read
    server-side from the platform env and never crosses the wire.
    """
    _require_manage(principal)
    adopt = _examlops_adopt()
    actor = principal.get("sub", "?")
    result = adopt.adopt_model(
        model,
        dry_run=bool(payload.get("dryRun", False)),
        connection_name=payload.get("connectionName", "minio"),
        provision_connection=bool(payload.get("provisionConnection", True)),
        actor=actor,
    )
    if not result.get("dry_run") and result.get("changed"):
        conn = _connect()
        _ensure_tables(conn)
        _audit(
            conn,
            actor,
            "project_onboarded",
            result["project"],
            {"via": "dashboard", "model": model, "steps": result["steps"]},
        )
        conn.commit()
        conn.close()
    return result


@router.post("/onboard-all")
async def onboard_all_view(
    payload: dict = Body(default={}),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PROJECT_MANAGE)),
) -> dict:
    """Onboard every Zoo/pack model (one project each; admin / project.manage; audited). Idempotent —
    already-provisioned models report ``changed=false``. Body (optional): ``{dryRun, connectionName,
    provisionConnection}``."""
    _require_manage(principal)
    adopt = _examlops_adopt()
    actor = principal.get("sub", "?")
    dry_run = bool(payload.get("dryRun", False))
    results = adopt.adopt_all(
        dry_run=dry_run,
        connection_name=payload.get("connectionName", "minio"),
        provision_connection=bool(payload.get("provisionConnection", True)),
        actor=actor,
    )
    changed = [r for r in results if not dry_run and r.get("changed")]
    if changed:
        conn = _connect()
        _ensure_tables(conn)
        _audit(
            conn,
            actor,
            "project_onboarded_bulk",
            "*",
            {"via": "dashboard", "count": len(changed), "models": [r["model"] for r in changed]},
        )
        conn.commit()
        conn.close()
    return {"results": results, "onboarded": len(changed)}

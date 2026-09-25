"""examlops.data.projects — projects.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
import os
from typing import Any  # noqa: F401

import examlops.platform_db as _pdb
from examlops.platform_db import (  # noqa: F401
    _PIPELINE_KINDS,
    _RESOURCE_KINDS,
    get_db,
    init_db,
    install_write_retry,
)

__all__ = [
    "add_project_member",
    "archive_project",
    "assign_model_to_project",
    "assign_resource_to_project",
    "bind_project_connection",
    "create_project",
    "delete_project",
    "ensure_project_storage",
    "get_project",
    "get_project_budget",
    "get_project_consumption",
    "get_project_for_model",
    "get_project_full",
    "get_project_pipelines",
    "get_project_storage",
    "list_project_budgets",
    "list_project_members",
    "list_project_models",
    "list_project_resources",
    "list_projects",
    "project_experiment",
    "projects_bucket",
    "refresh_project_usage",
    "remove_project_member",
    "remove_project_resource",
    "set_project_budget",
    "set_project_usage",
    "update_project_quota",
    "upsert_project_pipeline",
]


def add_project_member(project: str, subject: str, role: str, actor: str | None = None) -> None:
    """Add a person to a project with an ``owner|editor|viewer`` role (wraps authz.grant)."""
    if role not in {"owner", "editor", "viewer"}:
        raise ValueError("role must be one of: owner, editor, viewer")
    from examlops.authz import grant as _grant

    _grant(subject, role, f"project:{project}", actor=actor)


def archive_project(name: str) -> bool:
    """Set project status to ARCHIVED. Returns True if found."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE projects SET status='ARCHIVED', updated_at=CURRENT_TIMESTAMP WHERE name=?",
            (name,),
        )
    return True


def assign_model_to_project(project: str, model: str, added_by: str | None = None) -> bool:
    """Assign a model to a project. Returns False if project not found.

    Dual-writes the legacy ``project_models`` table and the unified ``project_resources``
    membership (ADR 0086, ``kind='model'``) so both stay consistent during the transition.
    """
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (project,)).fetchone()
        if not row:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO project_models (project, model) VALUES (?,?)",
            (project, model),
        )
        conn.execute(
            """INSERT OR REPLACE INTO project_resources (project, kind, ref, added_by)
               VALUES (?, 'model', ?, ?)""",
            (project, model, added_by),
        )
    return True


def assign_resource_to_project(
    project: str, kind: str, ref: str, added_by: str | None = None
) -> bool:
    """Attach any resource (by ``kind``/``ref``) to a project. False if project not found.

    ``kind='model'`` also mirrors into the legacy ``project_models`` table for back-compat.
    """
    if kind not in _RESOURCE_KINDS:
        raise ValueError(
            f"unknown resource kind: {kind!r} (expected one of {sorted(_RESOURCE_KINDS)})"
        )
    if kind == "model":
        return assign_model_to_project(project, ref, added_by=added_by)
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (project,)).fetchone()
        if not row:
            return False
        conn.execute(
            """INSERT OR REPLACE INTO project_resources (project, kind, ref, added_by)
               VALUES (?,?,?,?)""",
            (project, kind, ref, added_by),
        )
    return True


def bind_project_connection(project: str, connection_ref: str, actor: str | None = None) -> bool:
    """Bind an existing P2 S3 connection as the project's storage backend.

    Validates the ref against the project's connections (kind ``s3``); the effective bucket then
    comes from the connection config. Never copies a secret. Returns False if the project has no
    storage record or the connection is missing/not s3.
    """
    from examlops.connections import get_connection

    init_db()
    if get_project_storage(project) is None and ensure_project_storage(project) is None:
        return False
    conn_rec = get_connection(connection_ref, project=project)
    if not conn_rec or conn_rec.get("kind") != "s3":
        return False
    bucket = str(conn_rec.get("config", {}).get("bucket") or projects_bucket())
    with get_db() as conn:
        conn.execute(
            "UPDATE project_storage SET connection_ref=?, bucket=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE project=?",
            (connection_ref, bucket, project),
        )
    _pdb.write_audit_event(
        "cli", actor, "project_storage_bind", project, {"connection_ref": connection_ref}
    )
    return True


def create_project(
    name: str,
    *,
    description: str | None = None,
    cpu_limit: float = 4.0,
    memory_limit_gb: float = 8.0,
    storage_gb: float = 50.0,
    gpu_limit: int = 0,
    created_by: str | None = None,
) -> None:
    """Create a new project with resource quotas."""
    init_db()
    network_name = f"examlops-{name}"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO projects
               (name, description, cpu_limit, memory_limit_gb, storage_gb, gpu_limit,
                network_name, created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                name,
                description,
                cpu_limit,
                memory_limit_gb,
                storage_gb,
                gpu_limit,
                network_name,
                created_by,
            ),
        )


def delete_project(name: str) -> bool:
    """Delete a project with its FULL cascade. Returns True if found.

    Removes membership (``project_models``/``project_resources``), the authz grants
    (``authz_relations`` on ``project:<name>``), and the per-project budget/storage/pipeline
    rows. The authz cascade is security-relevant: leaving grants behind means re-creating a
    project with the same name silently resurrects every previous member's role. This is the
    ONE delete path — the dashboard router calls it too (Phase 42 shared-code-path rule).
    Underlying models/connections themselves are never deleted, only the grouping.
    """
    init_db()
    from examlops.authz import openfga_client as _fga

    cfg = _fga.config_from_env()
    if cfg is not None and get_project(name):
        # ADR 0014: with OpenFGA configured its tuples are the ones that decide, so the same
        # resurrection hazard applies there - drop them before the native rows.
        from examlops.data.governance import list_relations

        for rel in list_relations(obj=f"project:{name}"):
            _fga.delete_grant(cfg, rel["subject"], rel["relation"], rel["object"])
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM project_models WHERE project=?", (name,))
        conn.execute("DELETE FROM project_resources WHERE project=?", (name,))
        conn.execute("DELETE FROM authz_relations WHERE object=?", (f"project:{name}",))
        for tbl in ("project_budgets", "project_storage", "project_pipelines"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE project=?", (name,))  # noqa: S608
            except Exception:  # noqa: BLE001 — optional table absent in an older DB
                pass
        conn.execute("DELETE FROM projects WHERE name=?", (name,))
    return True


def ensure_project_storage(project: str) -> dict[str, Any] | None:
    """Upsert the default storage record for a *known* project; idempotent.

    Returns the record, or None if the project does not exist (never creates a row for an
    unknown project — R4). The default location is ``s3://<projects_bucket>/<project>/`` with a
    ``quota_gb`` mirrored from ``projects.storage_gb``.
    """
    init_db()
    proj = get_project(project)
    if not proj:
        return None
    with get_db() as conn:
        existing = conn.execute(
            "SELECT connection_ref, used_bytes FROM project_storage WHERE project=?", (project,)
        ).fetchone()
        conn.execute(
            """INSERT INTO project_storage
                   (project, bucket, prefix, connection_ref, quota_gb, used_bytes, updated_at)
               VALUES (?,?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(project) DO UPDATE SET
                   bucket=excluded.bucket, prefix=excluded.prefix,
                   quota_gb=excluded.quota_gb, updated_at=CURRENT_TIMESTAMP""",
            (
                project,
                projects_bucket(),
                f"{project}/",
                existing["connection_ref"] if existing else None,
                proj.get("storage_gb"),
                existing["used_bytes"] if existing else 0,
            ),
        )
    return get_project_storage(project)


def get_project(name: str) -> dict[str, Any] | None:
    """Return project row as dict, or None if not found."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def get_project_budget(project: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM project_budgets WHERE project=?", (project,)).fetchone()
    return dict(row) if row else None


def get_project_consumption(project: str, since: str | None = None) -> dict[str, float]:
    """Sum recorded GPU-hours and cost attributed to a project (ADR 0086).

    Resolves the project's models from the unified membership tables — ``project_resources``
    (kind='model') ∪ ``project_models`` ∪ ``namespace_models`` (back-compat alias) — so budgets
    enforce against the real training spend tracked by ``exa models cost``, whether a model was
    grouped via the new Projects surface or the legacy namespace surface.

    ``since`` is an ISO-8601 UTC timestamp: only costs recorded at or after it are counted. A
    budget has a *period* (ADR 0089), and comparing a monthly budget against every cost ever
    recorded made it breach permanently. Omitted, the sum is lifetime, as before.
    """
    init_db()
    clause = " AND c.recorded_at >= ?" if since else ""
    params: tuple = (project, project, project) + ((since,) if since else ())
    with get_db() as conn:
        row = conn.execute(
            f"""WITH members(model) AS (
                   SELECT ref  FROM project_resources WHERE project = ? AND kind = 'model'
                   UNION SELECT model FROM project_models   WHERE project   = ?
                   UNION SELECT model FROM namespace_models WHERE namespace = ?
               )
               SELECT COALESCE(SUM(c.gpu_hours), 0) AS gpu_hours,
                      COALESCE(SUM(c.cost_usd), 0)  AS cost_usd
               FROM members m
               JOIN model_costs c ON c.model_name = m.model
               WHERE 1=1{clause}""",
            params,
        ).fetchone()
    return {"gpu_hours": float(row["gpu_hours"]), "cost_usd": float(row["cost_usd"])}


def get_project_for_model(model: str) -> str | None:
    """Return the first project a model belongs to, or None (used for cost attribution)."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            """SELECT project FROM project_resources WHERE kind='model' AND ref=?
               UNION SELECT project FROM project_models WHERE model=?
               LIMIT 1""",
            (model, model),
        ).fetchone()
    return row["project"] if row else None


def get_project_full(name: str, *, live: Any = None) -> dict[str, Any] | None:
    """Full project anatomy for ``exa project show`` and the dashboard detail endpoint.

    Returns None for an unknown project (ADR 0086 R5). ``live`` is passed to
    :func:`get_project_pipelines` (ADR 0092 live hydration).
    """
    project = get_project(name)
    if not project:
        return None

    # P8 anatomy (ADR 0093): storage / connections / pipelines are fail-open and secret-safe —
    # a missing or unreachable source yields empty/None, never an exception. Storage is ensured
    # lazily so a real project always shows its location (idempotent; project is known here).
    try:
        storage = get_project_storage(name) or ensure_project_storage(name)
    except Exception:
        storage = None
    try:
        from examlops.connections import list_connections

        connections = [
            {"name": c["name"], "kind": c.get("kind"), "has_secret": bool(c.get("has_secret"))}
            for c in list_connections(project=name)
        ]
    except Exception:
        connections = []
    try:
        pipelines = get_project_pipelines(name, live=live)
    except Exception:
        pipelines = {"prefect": None, "rayserve": None}

    return {
        **project,
        "resources": list_project_resources(name),
        "members": list_project_members(name),
        "budget": get_project_budget(name),
        "consumption": get_project_consumption(name),
        "storage": storage,
        "connections": connections,
        "pipelines": pipelines,
    }


def get_project_pipelines(project: str, *, live: Any = None) -> dict[str, Any]:
    """Return the project's two pipeline surfaces (aggregation over existing state + registry).

    ``live`` is an ``examlops.project_pipelines.LiveSources`` naming the Prefect API / Ray Serve
    URLs to hydrate from (ADR 0092 decision 1); ``None`` uses the sources the environment names
    explicitly (``PREFECT_API_URL`` / ``RAY_SERVE_URL``) and contacts nothing when it names none.

    Fail-open: if a live source is unavailable the registry row is used (``source: "registry"``);
    an empty project yields both surfaces as None (never an error). Without a live source the
    Prefect surface's members are the project's models (their per-model deployments) and the Ray
    Serve surface's members are the same models with their traffic split from ``traffic_rules``.
    """
    base = _registry_pipelines(project)
    try:
        from examlops import project_pipelines as _live

        src = live if live is not None else _live.sources_from_env()
        if not src.enabled:
            return base
        return _live.hydrate(project, list_project_models(project), base, src)
    except Exception as exc:  # noqa: BLE001 — live hydration is an overlay; the registry view stands
        # hydrate() itself never raises, so reaching here is a bug (import error, bad ``live``):
        # keep the page up, but leave a trace instead of silently serving the registry forever.
        import logging

        logging.getLogger(__name__).warning(
            "project_pipelines: live overlay skipped for %s: %s", project, exc
        )
        return base


def _registry_pipelines(project: str) -> dict[str, Any]:
    """The registry/membership-only view of both surfaces (the fail-open floor)."""
    init_db()
    models = list_project_models(project)
    with get_db() as conn:
        reg = {
            r["kind"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM project_pipelines WHERE project=?", (project,)
            ).fetchall()
        }

    prefect_reg = reg.get("prefect")
    rayserve_reg = reg.get("rayserve")

    prefect = None
    if models or prefect_reg:
        prefect = {
            "deployments": [f"examlops-{m.lower()}" for m in models],
            "schedule": (prefect_reg or {}).get("schedule"),
            "last_run_at": (prefect_reg or {}).get("last_run_at"),
            "status": (prefect_reg or {}).get("status", "unknown"),
            "source": "registry",
        }
        try:
            store = get_project_storage(project)
        except Exception:  # noqa: BLE001 — storage is decoration on this surface; fail-open
            store = None
        if store:
            # ADR 0091 §3: the training surface's artifact destination is the project prefix.
            prefect["storage_prefix"] = f"s3://{store['bucket']}/{store['prefix']}"

    rayserve = None
    if models or rayserve_reg:
        traffic: dict[str, Any] = {}
        for m in models:
            rules = _pdb.get_traffic_rules(m)
            if rules:
                traffic[m] = rules
        rayserve = {
            "models": models,
            "traffic": traffic,
            "status": (rayserve_reg or {}).get("status", "unknown"),
            "source": "registry",
        }

    return {"prefect": prefect, "rayserve": rayserve}


def get_project_storage(project: str) -> dict[str, Any] | None:
    """Return the storage record for a project, or None."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM project_storage WHERE project=?", (project,)).fetchone()
    return dict(row) if row else None


def list_project_budgets() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM project_budgets ORDER BY project").fetchall()
    return [dict(r) for r in rows]


def list_project_members(project: str) -> list[dict[str, Any]]:
    """Return the people granted a relation directly on ``project:<name>``."""
    rows = _pdb.list_relations(obj=f"project:{project}")
    return [
        {
            "subject": r["subject"],
            "role": r["relation"],
            "granted_by": r.get("actor"),
            "when": r.get("created_at"),
        }
        for r in rows
    ]


def list_project_models(project: str) -> list[str]:
    """Return model names assigned to a project (unified: project_resources ∪ project_models)."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ref AS model FROM project_resources WHERE project=? AND kind='model'
               UNION SELECT model FROM project_models WHERE project=?
               ORDER BY model""",
            (project, project),
        ).fetchall()
    return [r["model"] for r in rows]


def list_project_resources(project: str, kind: str | None = None) -> dict[str, list[str]]:
    """Return a project's resources grouped by kind ({kind: [ref, ...]})."""
    init_db()
    with get_db() as conn:
        if kind:
            rows = conn.execute(
                "SELECT kind, ref FROM project_resources WHERE project=? AND kind=? ORDER BY ref",
                (project, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, ref FROM project_resources WHERE project=? ORDER BY kind, ref",
                (project,),
            ).fetchall()
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["kind"], []).append(r["ref"])
    # models also come from the legacy table (union)
    if kind in (None, "model"):
        legacy = {m for m in list_project_models(project)}
        grouped["model"] = sorted(set(grouped.get("model", [])) | legacy)
        if not grouped["model"]:
            grouped.pop("model", None)
    return grouped


def list_projects(status: str | None = None) -> list[dict[str, Any]]:
    """Return all projects, optionally filtered by status (ACTIVE/ARCHIVED)."""
    init_db()
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status=? ORDER BY name", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def project_experiment(project: str) -> str:
    """Stable MLflow experiment name whose artifact_location is the project prefix."""
    return f"project/{project}"


def projects_bucket() -> str:
    """The shared bucket that holds every project's prefix (env-overridable)."""
    return os.getenv("EXAMLOPS_PROJECTS_BUCKET", "examlops-projects")


def refresh_project_usage(project: str) -> int:
    """Probe MinIO for the bytes under the project prefix and store them; fail-open.

    Sums object sizes under ``<prefix>`` via S3 ListObjectsV2 (boto3, lazily imported). If MinIO
    or boto3 is unavailable, the prior ``used_bytes`` is kept and returned — the platform never
    raises on a missing backend (R5/R11).
    """
    rec = get_project_storage(project)
    if rec is None:
        return 0
    prior = int(rec.get("used_bytes") or 0)
    try:
        import boto3  # noqa: PLC0415  (lazy — MinIO/boto3 is optional)

        endpoint = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:19000")
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        )
        total = 0
        token = None
        while True:
            kw = {"Bucket": rec["bucket"], "Prefix": rec["prefix"]}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            total += sum(o.get("Size", 0) for o in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        set_project_usage(project, total)
        return total
    except Exception:
        return prior


def remove_project_member(
    project: str, subject: str, role: str | None = None, actor: str | None = None
) -> int:
    """Remove a person's grant(s) on a project (wraps authz.revoke). Returns rows removed."""
    from examlops.authz import revoke as _revoke

    roles = [role] if role else ["owner", "editor", "viewer"]
    return sum(_revoke(subject, r, f"project:{project}", actor=actor) for r in roles)


def remove_project_resource(project: str, kind: str, ref: str) -> bool:
    """Detach a resource from a project. Returns True if a row was removed."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM project_resources WHERE project=? AND kind=? AND ref=?",
            (project, kind, ref),
        )
        if kind == "model":
            conn.execute("DELETE FROM project_models WHERE project=? AND model=?", (project, ref))
        removed = cur.rowcount > 0
    return removed


def set_project_budget(
    project: str,
    gpu_hours_budget: float | None,
    cost_budget: float | None,
    period: str = "monthly",
    updated_by: str | None = None,
) -> None:
    """Set a project's budget, keeping its alert state (ADR 0089).

    An upsert of the budget columns only: ``INSERT OR REPLACE`` would drop the alert state, and an
    operator raising a budget above current spend is exactly when the recovery has to be noticed.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO project_budgets
               (project, gpu_hours_budget, cost_budget, period, updated_by, updated_at)
               VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(project) DO UPDATE SET
                   gpu_hours_budget=excluded.gpu_hours_budget,
                   cost_budget=excluded.cost_budget,
                   period=excluded.period,
                   updated_by=excluded.updated_by,
                   updated_at=CURRENT_TIMESTAMP""",
            (project, gpu_hours_budget, cost_budget, period, updated_by),
        )


def get_project_budget_alert(project: str) -> dict[str, Any] | None:
    """The last alert state recorded for a project's budget (ADR 0089), or ``None``."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT alert_state, alert_breaches_json, alerted_at FROM project_budgets "
            "WHERE project=?",
            (project,),
        ).fetchone()
    if row is None or row["alert_state"] is None:
        return None
    return {
        "state": row["alert_state"],
        "breaches": json.loads(row["alert_breaches_json"] or "[]"),
        "at": row["alerted_at"],
    }


def set_project_budget_alert(project: str, state: str, breaches: list[str]) -> None:
    """Record the alert state a breach event was raised for (ADR 0089)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE project_budgets SET alert_state=?, alert_breaches_json=?, "
            "alerted_at=CURRENT_TIMESTAMP WHERE project=?",
            (state, json.dumps(breaches), project),
        )


def set_project_usage(project: str, used_bytes: int) -> None:
    """Record a measured usage figure (called by refresh_project_usage / probes)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE project_storage SET used_bytes=?, updated_at=CURRENT_TIMESTAMP WHERE project=?",
            (int(used_bytes), project),
        )


def update_project_quota(
    name: str,
    *,
    cpu_limit: float | None = None,
    memory_limit_gb: float | None = None,
    storage_gb: float | None = None,
    gpu_limit: int | None = None,
    description: str | None = None,
    network_name: str | None = None,
) -> bool:
    """Update quota fields for a project. Returns True if found and updated.

    ``network_name`` sets the project's isolated namespace / network (the boundary a
    Compose/K8s runtime binds resources into). Pass an empty string to clear it.
    """
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        if network_name is not None:
            conn.execute(
                "UPDATE projects SET network_name=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (network_name or None, name),
            )
        if cpu_limit is not None:
            conn.execute(
                "UPDATE projects SET cpu_limit=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (cpu_limit, name),
            )
        if memory_limit_gb is not None:
            conn.execute(
                "UPDATE projects SET memory_limit_gb=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (memory_limit_gb, name),
            )
        if storage_gb is not None:
            conn.execute(
                "UPDATE projects SET storage_gb=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (storage_gb, name),
            )
        if gpu_limit is not None:
            conn.execute(
                "UPDATE projects SET gpu_limit=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (gpu_limit, name),
            )
        if description is not None:
            conn.execute(
                "UPDATE projects SET description=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (description, name),
            )
    return True


def upsert_project_pipeline(
    project: str,
    kind: str,
    ref: str,
    *,
    status: str = "unknown",
    schedule: str | None = None,
    last_run_at: str | None = None,
) -> None:
    """Register/update one of a project's two pipeline surfaces (PK project+kind = one-each)."""
    if kind not in _PIPELINE_KINDS:
        raise ValueError(f"kind must be one of {_PIPELINE_KINDS}, got {kind!r}")
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO project_pipelines
                   (project, kind, ref, status, schedule, last_run_at, updated_at)
               VALUES (?,?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(project, kind) DO UPDATE SET
                   ref=excluded.ref, status=excluded.status, schedule=excluded.schedule,
                   last_run_at=excluded.last_run_at, updated_at=CURRENT_TIMESTAMP""",
            (project, kind, ref, status, schedule, last_run_at),
        )


install_write_retry(__name__)

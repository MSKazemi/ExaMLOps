"""``exa modelzoo adopt`` — provision one project per Model Zoo model (ADR 0086 composition).

Makes "a project per model" the zero-effort default: for a Zoo/pack model it composes the existing
project primitives — ``create_project`` → ``ensure_project_storage`` → ``set_project_budget`` →
``assign_model_to_project`` → ``create_workbench`` → register the two pipeline surfaces — **idempotently**.

Not forced: a project can still hold several models (champion+challenger, ensembles) via
``exa project assign``; ``adopt`` just removes the setup friction for the common one-model case. It
does **not** itself deploy flows to Prefect/Ray — it wires the project and records the pipeline
surfaces so the anatomy view is complete; ``exa pipeline deploy`` ships the actual flows. Every real
provisioning run writes one ``modelzoo_adopt`` audit event.
"""

from __future__ import annotations

from typing import Any

# Default quota/budget for an auto-adopted project (matches create_project's own defaults +
# a conservative monthly budget). Overridable per call.
DEFAULT_CPU_LIMIT = 4.0
DEFAULT_MEMORY_GB = 8.0
DEFAULT_STORAGE_GB = 50.0
DEFAULT_GPU_HOURS_BUDGET = 100.0
DEFAULT_COST_BUDGET = 250.0
WORKBENCH_NAME = "nb1"
DEFAULT_CONNECTION_NAME = "minio"


def _resolve_s3_config(
    endpoint: str | None = None,
    access_key: str | None = None,
    secret: str | None = None,
    bucket: str | None = None,
) -> dict[str, str | None] | None:
    """Resolve the per-project S3/MinIO connection config from explicit args or the platform env.

    Returns ``{endpoint, access_key, secret, bucket}`` or ``None`` when no S3 endpoint can be found
    (so the caller skips connection provisioning rather than creating a useless connection). The
    secret is read from the environment only — never hardcoded — and passed straight to the D7
    secrets store by :func:`examlops.connections.create_connection`.
    """
    import os

    from examlops.data.projects import projects_bucket

    endpoint = (
        endpoint or os.getenv("MLFLOW_S3_ENDPOINT_URL") or os.getenv("EXAMLOPS_DATA_S3_ENDPOINT")
    )
    if not endpoint:
        return None
    return {
        "endpoint": endpoint,
        "access_key": access_key or os.getenv("AWS_ACCESS_KEY_ID") or "minioadmin",
        "secret": secret if secret is not None else os.getenv("AWS_SECRET_ACCESS_KEY"),
        "bucket": bucket or projects_bucket(),
    }


def zoo_models() -> list[str]:
    """Every model the active use-case pack declares (its ``models/*.yaml``), Zoo-order."""
    from examlops.usecase import models_dir

    d = models_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.yaml")):
        if p.stem.startswith("_"):
            continue
        try:
            import yaml

            name = (yaml.safe_load(p.read_text()) or {}).get("name") or p.stem
        except Exception:
            name = p.stem
        out.append(str(name))
    return out


def project_name_for(model: str) -> str:
    """The canonical per-model project name (lowercased, matching the MLflow-registry convention)."""
    return model.strip().lower()


def _pending_steps(
    project: str, model: str, connection_name: str = DEFAULT_CONNECTION_NAME
) -> dict[str, bool]:
    """Which provisioning steps are still needed for ``(project, model)`` (for --dry-run + idempotency)."""
    from examlops.connections import get_connection
    from examlops.data import get_db, init_db
    from examlops.data.projects import (
        get_project,
        get_project_budget,
        get_project_storage,
        list_project_models,
    )
    from examlops.workbenches import get_workbench

    init_db()
    with get_db() as conn:
        pipe_kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM project_pipelines WHERE project=?", (project,)
            ).fetchall()
        }

    # Each step is pending iff its artifact is absent — the store getters return None/[] for an
    # unknown project, so a brand-new project correctly reports every sub-step as pending.
    return {
        "project": get_project(project) is None,
        "storage": get_project_storage(project) is None,
        "connection": get_connection(connection_name, project=project) is None,
        "budget": get_project_budget(project) is None,
        "model": model not in list_project_models(project),
        "workbench": get_workbench(WORKBENCH_NAME, project) is None,
        "pipelines": pipe_kinds != {"prefect", "rayserve"},
    }


def adopt_model(
    model: str,
    *,
    cpu_limit: float = DEFAULT_CPU_LIMIT,
    memory_gb: float = DEFAULT_MEMORY_GB,
    storage_gb: float = DEFAULT_STORAGE_GB,
    gpu_hours_budget: float | None = DEFAULT_GPU_HOURS_BUDGET,
    cost_budget: float | None = DEFAULT_COST_BUDGET,
    connection_name: str = DEFAULT_CONNECTION_NAME,
    provision_connection: bool = True,
    s3_endpoint: str | None = None,
    s3_access_key: str | None = None,
    s3_secret: str | None = None,
    s3_bucket: str | None = None,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Provision (or complete) the project for one model. Idempotent; safe to re-run.

    Composes the project primitives — project envelope → storage → **per-project MinIO/S3 connection
    (bound to that storage)** → budget → model membership → workbench → pipeline surfaces. The
    connection step resolves its endpoint/keys from explicit args or the platform S3 env
    (``MLFLOW_S3_ENDPOINT_URL``/``AWS_*``); when no endpoint is configured it is recorded as
    ``"skipped"`` rather than failing. Set ``provision_connection=False`` to skip it entirely.

    Returns ``{model, project, dry_run, changed, steps}`` where ``steps`` maps each provisioning
    step to ``"created"`` / ``"exists"`` / ``"skipped"`` (or the ``"would-*"`` forms under dry-run).
    """
    from examlops.platform_db import _actor

    actor = actor or _actor()
    project = project_name_for(model)
    pending = _pending_steps(project, model, connection_name)
    steps: dict[str, str] = {}

    if dry_run:
        for name, needed in pending.items():
            steps[name] = "would-create" if needed else "exists"
        # The connection step is a no-op when disabled or when no S3 endpoint is resolvable.
        if steps.get("connection") == "would-create" and (
            not provision_connection
            or _resolve_s3_config(s3_endpoint, s3_access_key, s3_secret, s3_bucket) is None
        ):
            steps["connection"] = "would-skip"
        return {
            "model": model,
            "project": project,
            "dry_run": True,
            "changed": any(pending.values()),
            "steps": steps,
        }

    from examlops.data.projects import (
        assign_model_to_project,
        bind_project_connection,
        create_project,
        ensure_project_storage,
        set_project_budget,
        upsert_project_pipeline,
    )
    from examlops.workbenches import create_workbench

    # 1. Project envelope (quota).
    if pending["project"]:
        create_project(
            project,
            description=f"Auto-adopted workspace for Zoo model {model}",
            cpu_limit=cpu_limit,
            memory_limit_gb=memory_gb,
            storage_gb=storage_gb,
            created_by=actor,
        )
        steps["project"] = "created"
    else:
        steps["project"] = "exists"

    # 2. Storage (bucket prefix + /project quota).
    ensure_project_storage(project)
    steps["storage"] = "created" if pending["storage"] else "exists"

    # 2b. Per-project MinIO/S3 connection, bound to the project's storage (the "connect minio" step).
    #     Idempotent: reuse an existing connection (just ensure it stays bound); create one only when
    #     an S3 endpoint is resolvable, else record "skipped" so offline/dev provisioning still works.
    if provision_connection:
        from examlops.connections import create_connection, get_connection

        if get_connection(connection_name, project=project) is not None:
            bind_project_connection(project, connection_name, actor=actor)
            steps["connection"] = "exists"
        elif (
            s3 := _resolve_s3_config(s3_endpoint, s3_access_key, s3_secret, s3_bucket)
        ) is not None:
            cfg = {
                "endpoint": s3["endpoint"],
                "bucket": s3["bucket"],
                "access_key": s3["access_key"],
            }
            try:
                create_connection(
                    connection_name,
                    "s3",
                    project=project,
                    config=cfg,
                    secret_value=s3["secret"],
                    created_by=actor,
                )
            except Exception:
                # Secrets store unavailable (no KEK configured): still register the connection
                # metadata without a stored secret so the per-project connection + binding exist
                # (S3 access then uses the ambient AWS_* env). The secret write happens before the
                # row insert, so no partial connection is left behind to retry over.
                create_connection(
                    connection_name,
                    "s3",
                    project=project,
                    config=cfg,
                    secret_value=None,
                    created_by=actor,
                )
            bind_project_connection(project, connection_name, actor=actor)
            steps["connection"] = "created"
        else:
            steps["connection"] = "skipped"
    else:
        steps["connection"] = "skipped"

    # 3. Budget (FinOps guardrail).
    if pending["budget"] and (gpu_hours_budget is not None or cost_budget is not None):
        set_project_budget(project, gpu_hours_budget, cost_budget, updated_by=actor)
        steps["budget"] = "created"
    else:
        steps["budget"] = "exists"

    # 4. Model membership (dual-writes project_models + project_resources).
    assign_model_to_project(project, model, added_by=actor)
    steps["model"] = "created" if pending["model"] else "exists"

    # 5. Workbench (Jupyter env for the project).
    if pending["workbench"]:
        create_workbench(WORKBENCH_NAME, project, created_by=actor)
        steps["workbench"] = "created"
    else:
        steps["workbench"] = "exists"

    # 6. Register the two pipeline surfaces (train + serve) so the anatomy view is complete.
    if pending["pipelines"]:
        upsert_project_pipeline(project, "prefect", f"train:{model}", status="scaffolded")
        upsert_project_pipeline(project, "rayserve", f"serve:{model}", status="scaffolded")
        steps["pipelines"] = "created"
    else:
        steps["pipelines"] = "exists"

    changed = any(v == "created" for v in steps.values())
    if changed:
        _audit_adopt(model, project, steps, actor)
    return {
        "model": model,
        "project": project,
        "dry_run": False,
        "changed": changed,
        "steps": steps,
    }


def adopt_all(
    *, dry_run: bool = False, actor: str | None = None, **kw: Any
) -> list[dict[str, Any]]:
    """Adopt every Zoo/pack model. Idempotent — already-provisioned models report ``changed=False``."""
    return [adopt_model(m, dry_run=dry_run, actor=actor, **kw) for m in zoo_models()]


def _audit_adopt(model: str, project: str, steps: dict[str, str], actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "exa-modelzoo",
            actor,
            "modelzoo_adopt",
            project,
            {"model": model, "steps": steps},
        )
    except Exception:  # pragma: no cover - audit must never block provisioning
        pass

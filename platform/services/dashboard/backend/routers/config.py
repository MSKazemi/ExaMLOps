"""Config router: GET masks secrets, PUT encrypts + audits."""

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from auth import require_role
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from models import DashboardAudit, DashboardConfig
from pydantic import BaseModel
from secret_store import decrypt, encrypt
from settings import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/config", tags=["config"])

# Whitelist of valid config keys. Editing this list is the only way to add
# new fields to the Config UI; PUT rejects anything not listed here.
URL_KEYS: frozenset[str] = frozenset(
    {
        "mlflow_url",
        "prefect_url",
        "ray_serve_url",
        "ray_dashboard_url",
        "prometheus_url",
        "grafana_url",
        "minio_url",
        "minio_console_url",
        # promotion thresholds + slurm fields shown in the UI today; treated
        # as URL-shape (plaintext) keys here even though they aren't URLs.
        "threshold_jpcp_rmse",
        "slurm_mode",
        "slurm_partition",
        "slurm_cpus_per_task",
        "slurm_mem",
        "slurm_time",
        # GitLab ModelZoo integration (non-sensitive)
        "gitlab_url",
        "gitlab_project_id",
        # Dataplane bus bridge
        "dataplane_bus_host",
        "dataplane_bus_port",
        "dataplane_bus_mode",
        "dataplane_bus_job_topic_uuid",
        "dataplane_bus_result_topic_uuid",
        "dataplane_bus_inference_uuid",
        "dataplane_bus_retrain_uuid",
        "dataplane_bus_default_model",
        "dataplane_bus_default_alias",
        "dataplane_bus_bridge_status_url",
        # JupyterHub
        "jupyterhub_url",
    }
)
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "minio_access_key",
        "minio_secret_key",
        "grafana_api_key",
        "control_plane_token",
        "gitlab_token",
        "gitlab_pipeline_token",
    }
)
ALL_KEYS: frozenset[str] = URL_KEYS | SECRET_KEYS

# Maps dashboard config key names → environment variable names for .env.dashboard export.
# Only keys listed here are written; all others (UI-only / dashboard-internal) are skipped.
ENV_VAR_MAP: dict[str, str] = {
    "mlflow_url": "MLFLOW_TRACKING_URI",
    "prefect_url": "PREFECT_API_URL",
    "ray_serve_url": "RAY_SERVE_URL",
    "minio_url": "MLFLOW_S3_ENDPOINT_URL",
    "minio_access_key": "AWS_ACCESS_KEY_ID",
    "minio_secret_key": "AWS_SECRET_ACCESS_KEY",
    "prometheus_url": "PROMETHEUS_URL",
    "grafana_url": "GRAFANA_URL",
    "grafana_api_key": "GRAFANA_API_KEY",
    "control_plane_token": "CONTROL_PLANE_TOKEN",
    "dataplane_bus_host": "DATAPLANE_BUS_HOST",
    "dataplane_bus_port": "DATAPLANE_BUS_PORT",
    "dataplane_bus_mode": "DATAPLANE_BUS_MODE",
    "dataplane_bus_job_topic_uuid": "DATAPLANE_BUS_JOB_TOPIC_UUID",
    "dataplane_bus_result_topic_uuid": "DATAPLANE_BUS_RESULT_TOPIC_UUID",
    "dataplane_bus_inference_uuid": "DATAPLANE_BUS_INFERENCE_UUID",
    "dataplane_bus_retrain_uuid": "DATAPLANE_BUS_RETRAIN_UUID",
    "dataplane_bus_default_model": "DATAPLANE_BUS_DEFAULT_MODEL",
    "dataplane_bus_default_alias": "DATAPLANE_BUS_DEFAULT_ALIAS",
    "slurm_mode": "EXAMLOPS_SLURM_MODE",
    "slurm_partition": "EXAMLOPS_SLURM_PARTITION",
    "slurm_cpus_per_task": "EXAMLOPS_SLURM_CPUS",
    "slurm_mem": "EXAMLOPS_SLURM_MEM",
    "slurm_time": "EXAMLOPS_SLURM_TIME",
}


class ConfigKeyMeta(BaseModel):
    key: str
    is_secret: bool
    has_value: bool
    updated_at: datetime


@router.get(
    "",
    summary="Read all config values; secrets are masked",
    description="Secret values are returned as the literal string '***' "
    "when set, or null when unset. Non-secret values are plaintext.",
)
async def get_config(
    _: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (await db.execute(select(DashboardConfig))).scalars().all()
    out: dict[str, Any] = {}
    for r in rows:
        if r.is_secret:
            out[r.key] = "***" if r.secret_value is not None else None
        else:
            out[r.key] = r.value
    return out


@router.get(
    "/keys",
    response_model=list[ConfigKeyMeta],
    summary="Return per-key metadata (no values)",
)
async def get_config_keys(
    _: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> list[ConfigKeyMeta]:
    rows = (await db.execute(select(DashboardConfig))).scalars().all()
    return [
        ConfigKeyMeta(
            key=r.key,
            is_secret=r.is_secret,
            has_value=(r.secret_value is not None) if r.is_secret else (r.value is not None),
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.put(
    "",
    summary="Update config values (admin only)",
    description=(
        "URL keys: any string stored as plaintext. Secret keys: blank string "
        "rejected (422); null clears (audit action=clear); non-empty string "
        "encrypts and stores (audit action=set). Unknown keys → 400."
    ),
)
async def put_config(
    updates: dict[str, Any],
    claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # 1. validate keys upfront (atomic: all-or-nothing)
    for k, v in updates.items():
        if k not in ALL_KEYS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown key: {k!r}")
        if k in SECRET_KEYS:
            if v is not None and not isinstance(v, str):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"secret key {k!r} requires string or null",
                )
            if v == "":
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"secret key {k!r}: blank string not allowed; use null to clear",
                )
        else:
            if not isinstance(v, str):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"key {k!r} requires string value",
                )

    # 2. apply
    for k, v in updates.items():
        row = await db.get(DashboardConfig, k)
        if k in SECRET_KEYS:
            if row is None:
                row = DashboardConfig(key=k, is_secret=True)
                db.add(row)
            if v is None:
                row.secret_value = None
                db.add(DashboardAudit(role="admin", action="clear", key=k))
            else:
                row.secret_value = encrypt(v)
                db.add(DashboardAudit(role="admin", action="set", key=k))
        else:
            if row is None:
                row = DashboardConfig(key=k, is_secret=False)
                db.add(row)
            row.value = v
            # No audit row for URL changes.

    await db.commit()

    # 3. return masked snapshot
    rows = (await db.execute(select(DashboardConfig))).scalars().all()
    out: dict[str, Any] = {}
    for r in rows:
        if r.is_secret:
            out[r.key] = "***" if r.secret_value is not None else None
        else:
            out[r.key] = r.value
    return out


@router.post(
    "/export-env",
    summary="Export all config as a .env.dashboard file (admin only)",
    description=(
        "Decrypts secrets and writes every mapped key to .env.dashboard. "
        "Returns the file as a browser download. Run docker-compose restart "
        "on affected services after downloading."
    ),
)
async def export_env(
    claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    rows = (await db.execute(select(DashboardConfig))).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No config saved — save config first")

    row_map = {r.key: r for r in rows}
    lines: list[str] = [
        "# Generated by ExaMLOps dashboard — do not edit manually.",
        "# Restart affected services after applying:",
        "#   docker-compose restart mlflow ray-serve control-plane dataplane-bus-bridge",
        "",
    ]

    exported_keys: list[str] = []
    for dashboard_key, env_var in ENV_VAR_MAP.items():
        row = row_map.get(dashboard_key)
        if row is None:
            continue
        if row.is_secret:
            if row.secret_value is None:
                continue
            value = decrypt(row.secret_value)
        else:
            if not row.value:
                continue
            value = row.value
        lines.append(f"{env_var}={value}")
        exported_keys.append(dashboard_key)

    content = "\n".join(lines) + "\n"

    # Audit the bulk export (D3): it decrypts every stored secret. Key NAMES only — never values.
    db.add(
        DashboardAudit(
            role="admin",
            action="config_exported",
            key=",".join(sorted(exported_keys)),
        )
    )
    await db.commit()

    # Write server-side copy so docker-compose env_file picks it up on restart.
    try:
        export_path = Path(settings.env_export_path)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(content)
        # The file holds decrypted secrets — owner-only, never the image default umask (D3).
        os.chmod(export_path, 0o600)
    except OSError:
        pass  # In test environments the path may not be writable — still return the download.

    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename=".env.dashboard"'},
    )


ENV_VAR_TO_KEY: dict[str, str] = {v: k for k, v in ENV_VAR_MAP.items()}


def _parse_dotenv(content: str) -> dict[str, str]:
    """Parse .env file content, returning {VAR: value} for non-comment lines."""
    result: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        var, _, val = line.partition("=")
        var = var.strip()
        val = val.strip()
        # strip optional surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if var:
            result[var] = val
    return result


class ImportEnvResult(BaseModel):
    imported: list[str]
    skipped: list[str]


@router.post(
    "/import-env",
    response_model=ImportEnvResult,
    summary="Import config from an uploaded .env file (admin only)",
    description=(
        "Parse a .env file and upsert matching keys into the config store. "
        "Unknown env-var names are skipped. Secret keys are encrypted at rest."
    ),
)
async def import_env(
    file: UploadFile,
    claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> ImportEnvResult:
    raw = (await file.read()).decode("utf-8", errors="replace")
    env_vars = _parse_dotenv(raw)

    imported: list[str] = []
    skipped: list[str] = []

    for env_var, value in env_vars.items():
        config_key = ENV_VAR_TO_KEY.get(env_var)
        if config_key is None:
            skipped.append(env_var)
            continue

        row = await db.get(DashboardConfig, config_key)
        if config_key in SECRET_KEYS:
            if not value:
                skipped.append(env_var)
                continue
            if row is None:
                row = DashboardConfig(key=config_key, is_secret=True)
                db.add(row)
            row.secret_value = encrypt(value)
            db.add(DashboardAudit(role="admin", action="set", key=config_key))
        else:
            if row is None:
                row = DashboardConfig(key=config_key, is_secret=False)
                db.add(row)
            row.value = value

        imported.append(config_key)

    await db.commit()
    return ImportEnvResult(imported=imported, skipped=skipped)


def get_decrypted_secret_sync_factory():
    """Helper for the proxy: returns a coroutine that yields the cleartext
    value of a secret key, or None when unset. Imported by routers/proxy.py."""

    async def _get(db: AsyncSession, key: str) -> str | None:
        row = await db.get(DashboardConfig, key)
        if row is None or row.secret_value is None:
            return None
        return decrypt(row.secret_value)

    return _get


get_decrypted_secret = get_decrypted_secret_sync_factory()

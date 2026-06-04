"""SeanerBUS bridge config and status endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
import yaml as _yaml
from auth import require_role
from database import get_db
from fastapi import APIRouter, Depends
from models import DashboardConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from routers.config import URL_KEYS

_DEFAULT_BRIDGE_STATUS_URL = os.getenv("SEANERBUS_BRIDGE_STATUS_URL", "http://localhost:8003")

log = logging.getLogger("dashboard.seanerbus")

router = APIRouter(prefix="/seanerbus", tags=["seanerbus"])

_SEANERBUS_KEYS: frozenset[str] = frozenset(k for k in URL_KEYS if k.startswith("seanerbus_"))


async def _get_seanerbus_config(db: AsyncSession) -> dict[str, str]:
    stmt = select(DashboardConfig).where(DashboardConfig.key.in_(_SEANERBUS_KEYS))
    rows = (await db.execute(stmt)).scalars().all()
    return {r.key: r.value for r in rows if r.value}


@router.get("/config")
async def get_seanerbus_config(
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("viewer")),
) -> dict:
    """Return all SeanerBUS config keys (plaintext only, no secrets)."""
    return await _get_seanerbus_config(db)


@router.get("/model-uuids")
async def get_model_uuids(
    _role: str = Depends(require_role("viewer")),
) -> dict:
    """Return {model_name: uuid_or_null} for all models in the YAML registry."""
    models_dir = os.getenv("MODELS_YAML_DIR", "pipelines/models")
    result: dict[str, str | None] = {}
    models_path = Path(models_dir)
    if not models_path.is_dir():
        return result
    for yaml_file in sorted(models_path.glob("*.yaml")):
        if yaml_file.stem.startswith("_"):
            continue
        try:
            raw = _yaml.safe_load(yaml_file.read_text()) or {}
            name = raw.get("name", yaml_file.stem)
            result[name] = raw.get("seanerbus_uuid") or None
        except Exception as exc:
            log.warning("Could not read %s: %s", yaml_file.name, exc)
    return result


_GRAFANA_DASHBOARD_UID = "examlops-seanerbus"
_GRAFANA_PANEL_IDS = {"bridge_up": 1, "inference_rate": 2, "error_rate": 3, "latency": 4}


@router.get("/grafana-panels")
async def get_grafana_panels(
    _role: str = Depends(require_role("viewer")),
) -> dict:
    """Return Grafana panel info for the SeanerBUS Live Metrics section."""
    grafana_url = os.getenv("PUBLIC_GRAFANA_URL", "http://localhost:13000").rstrip("/")
    return {
        "grafana_url": grafana_url,
        "dashboard_uid": _GRAFANA_DASHBOARD_UID,
        "panels": _GRAFANA_PANEL_IDS,
    }


@router.get("/status")
async def get_seanerbus_status(
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("viewer")),
) -> dict:
    """Probe the bridge status server and return health + stats."""
    cfg = await _get_seanerbus_config(db)
    status_url = cfg.get("seanerbus_bridge_status_url") or _DEFAULT_BRIDGE_STATUS_URL

    result: dict = {"reachable": False, "status_url": status_url, "health": None, "stats": None}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            health_resp, stats_resp = await asyncio.gather(
                client.get(f"{status_url}/health"),
                client.get(f"{status_url}/stats"),
            )
            result["reachable"] = True
            result["health"] = health_resp.json()
            result["stats"] = stats_resp.json()
    except Exception as exc:
        result["error"] = str(exc)
        log.debug("Bridge status probe failed: %s", exc)

    return result

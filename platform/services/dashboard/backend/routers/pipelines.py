"""Pipelines router — Prefect deployment status and flow run triggering."""

from __future__ import annotations

import asyncio
import logging
import time

import audit_write
import httpx
from auth import require_role
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from settings import settings

log = logging.getLogger("dashboard.pipelines")

# In-process double-fire guard: a double-clicked Trigger button (or an impatient retry) must not
# launch two identical training jobs on the HPC allocation. Coarse by design — the durable
# cross-process dedup lives in the control plane's /retrain path; this covers the direct
# Prefect trigger this router performs.
_TRIGGER_COOLDOWN_S = 30.0
_recent_triggers: dict[str, float] = {}

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

_viewer = require_role("viewer")
_admin = require_role("admin")


def _prefect_base() -> str:
    return settings.prefect_url.rstrip("/") + "/api"


async def _post(path: str, body: dict) -> list | dict:
    async with httpx.AsyncClient(base_url=_prefect_base(), timeout=10.0) as client:
        r = await client.post(path, json=body)
    if r.status_code >= 400:
        raise HTTPException(502, f"Prefect {r.status_code}: {r.text[:200]}")
    return r.json()


async def _get(path: str) -> dict:
    async with httpx.AsyncClient(base_url=_prefect_base(), timeout=10.0) as client:
        r = await client.get(path)
    if r.status_code == 404:
        raise HTTPException(404, f"Prefect resource not found: {path}")
    if r.status_code >= 400:
        raise HTTPException(502, f"Prefect {r.status_code}: {r.text[:200]}")
    return r.json()


@router.get("/deployments")
async def list_deployments(_=Depends(_viewer)) -> list[dict]:
    """List all Prefect deployments."""
    result = await _post("/deployments/filter", {})
    return result if isinstance(result, list) else []


@router.get("/runs")
async def list_runs(limit: int = 20, _=Depends(_viewer)) -> list[dict]:
    """List recent flow runs, newest first."""
    result = await _post("/flow_runs/filter", {"limit": limit, "sort": "EXPECTED_START_TIME_DESC"})
    return result if isinstance(result, list) else []


class TriggerBody(BaseModel):
    model_name: str
    dataset_name: str | None = None
    dummy: bool = False


@router.post("/trigger")
async def trigger_run(body: TriggerBody, claims: dict = Depends(_admin)) -> dict:
    """Trigger a Prefect flow run for a model's registered deployment (audited, dedup-guarded)."""
    key = body.model_name.lower()
    now = time.monotonic()
    last = _recent_triggers.get(key)
    if last is not None and now - last < _TRIGGER_COOLDOWN_S:
        raise HTTPException(
            429,
            f"a training run for {body.model_name!r} was triggered "
            f"{now - last:.0f}s ago — wait {_TRIGGER_COOLDOWN_S:.0f}s between triggers",
        )
    dep_slug = f"examlops-{body.model_name.lower()}-nightly"
    dep = await _get(f"/deployments/name/training_flow/{dep_slug}")
    dep_id = dep.get("id")
    if not dep_id:
        raise HTTPException(502, "Prefect returned deployment with no id")
    params: dict = {"model_name": body.model_name, "is_dummy": body.dummy}
    if body.dataset_name:
        params["dataset_name"] = body.dataset_name
    result = await _post(f"/deployments/{dep_id}/create_flow_run", {"parameters": params})
    data = result if isinstance(result, dict) else {}
    _recent_triggers[key] = now
    # A training run launched from the UI is a retrain by another door — it must appear in
    # `exa audit` like every other trigger source (bridge, control plane, autopilot, CLI).
    await asyncio.to_thread(
        audit_write.audit,
        claims.get("sub", claims.get("role", "?")),
        "retrain_triggered",
        body.model_name,
        {
            "via": "dashboard-pipelines",
            "flow_run_id": data.get("id"),
            "dataset": body.dataset_name,
            "dummy": body.dummy,
        },
    )
    return {
        "flow_run_id": data.get("id"),
        "state": (data.get("state") or {}).get("type"),
    }

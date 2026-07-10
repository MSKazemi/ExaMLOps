"""Pipelines router — Prefect deployment status and flow run triggering."""

from __future__ import annotations

import logging

import httpx
from auth import require_role
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from settings import settings

log = logging.getLogger("dashboard.pipelines")

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
async def trigger_run(body: TriggerBody, _=Depends(_admin)) -> dict:
    """Trigger a Prefect flow run for a model's registered deployment."""
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
    return {
        "flow_run_id": data.get("id"),
        "state": (data.get("state") or {}).get("type"),
    }

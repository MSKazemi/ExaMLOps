"""Pipelines router — Prefect deployment status and flow run triggering."""

from __future__ import annotations

import asyncio
import logging
import uuid

import audit_write
import httpx
from auth import require_role
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from settings import settings
from upstream import dashboard_status

log = logging.getLogger("dashboard.pipelines")

# In-process double-fire guard: a double-clicked Trigger button (or an impatient retry) must not
# launch two identical training jobs on the HPC allocation. Coarse by design — the durable
# cross-process dedup lives in the control plane's /retrain path; this covers the direct
# Prefect trigger this router performs.

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

_viewer = require_role("viewer")
_admin = require_role("admin")


def _prefect_auth() -> dict[str, str]:
    """Prefect's API auth string / key when the server requires one (plan P3.6)."""
    from examlops.service_auth import prefect_headers

    return prefect_headers()


def _prefect_base() -> str:
    return settings.prefect_url.rstrip("/") + "/api"


async def _post(path: str, body: dict) -> list | dict:
    async with httpx.AsyncClient(
        base_url=_prefect_base(), timeout=10.0, headers=_prefect_auth()
    ) as client:
        r = await client.post(path, json=body)
    if r.status_code >= 400:
        raise HTTPException(502, f"Prefect {r.status_code}: {r.text[:200]}")
    return r.json()


async def _get(path: str) -> dict:
    async with httpx.AsyncClient(
        base_url=_prefect_base(), timeout=10.0, headers=_prefect_auth()
    ) as client:
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


async def _control_plane(method: str, path: str, **kwargs) -> httpx.Response:
    """One call to the control plane with the dashboard's credential (patchable in tests)."""
    from control_plane_auth import control_plane_token  # noqa: PLC0415

    token = await control_plane_token()
    headers = dict(kwargs.pop("headers", {}) or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=settings.control_plane_url, timeout=10.0) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def _problem_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"control plane answered HTTP {resp.status_code}"
    return str(body.get("detail") or body.get("title") or resp.status_code)


@router.post("/trigger")
async def trigger_run(body: TriggerBody, claims: dict = Depends(_admin)) -> dict:
    """Queue a retrain through the control plane (``POST /v1/retrain``), audited.

    This used to call Prefect directly: it bypassed the control plane's admission, audit and
    dedup, kept its own per-process cooldown (a fourth dedup mechanism), and targeted
    ``training_flow/examlops-<model>-nightly`` — a deployment that does not exist, with a
    ``dataset_name`` parameter the flow does not accept (plan P1.7). It now submits the same
    asynchronous command `exa retrain --async` does, so one training lease governs every door.
    """
    dataset = body.dataset_name
    if not dataset:
        models = await _control_plane("GET", "/v1/models")
        if models.status_code >= 400:
            raise HTTPException(dashboard_status(models.status_code), _problem_detail(models))
        entry = next((m for m in models.json() if m.get("model_name") == body.model_name), None)
        if entry is None or not entry.get("datasets"):
            raise HTTPException(400, f"Unknown model {body.model_name!r} or it has no datasets")
        dataset = entry["datasets"][0]
    resp = await _control_plane(
        "POST",
        "/v1/retrain",
        json={"model_name": body.model_name, "dataset_name": dataset, "is_dummy": body.dummy},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    if resp.status_code >= 400:
        # 409 = a retrain of this model × dataset is already queued or training (the lease).
        raise HTTPException(dashboard_status(resp.status_code), _problem_detail(resp))
    command = resp.json()
    await asyncio.to_thread(
        audit_write.audit,
        claims.get("sub", claims.get("role", "?")),
        "retrain_triggered",
        body.model_name,
        {
            "via": "dashboard-pipelines",
            "command_id": command.get("command_id"),
            "dataset": dataset,
            "dummy": body.dummy,
        },
    )
    return {
        "command_id": command.get("command_id"),
        "status_url": command.get("status_url"),
        "state": command.get("state"),
        "flow_run_id": (command.get("result") or {}).get("flow_run_id"),
    }

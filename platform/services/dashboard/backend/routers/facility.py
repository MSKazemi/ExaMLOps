"""Exascale facility-console BFF endpoints (F6 / ADR 0059).

Scheduler-neutral, view-shaped ``/api/v1/facility/*`` endpoints over the ``hpc_jobs`` table
(phase-23 scheduler abstraction). Composed through the F8 BFF substrate (per-source timeout +
``_partial`` fallback), viewer-gated, and graceful on missing telemetry (F6 R7). A ``?cluster=``
query rescopes every list for the multi-cluster switcher (F6 R6).
"""

from __future__ import annotations

from typing import Any

import facility
from auth import require_role
from bff import aggregate
from dbconn import platform_db_path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/v1/facility", tags=["facility"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _platform_db_path() -> str:
    return platform_db_path()


@router.get("/overview")
async def overview(cluster: str | None = None, _=Depends(_viewer)) -> dict[str, Any]:
    """Facility KPIs + per-partition utilization (F6 R1)."""
    db = _platform_db_path()

    def _ov() -> dict[str, Any]:
        return facility.facility_overview(db, scheduler=cluster)

    return await aggregate({"facility": _ov})


@router.get("/queue")
async def queue(cluster: str | None = None, _=Depends(_viewer)) -> dict[str, Any]:
    """Waiting jobs, longest-waiting first (F6 R2)."""
    db = _platform_db_path()

    def _q() -> dict[str, Any]:
        jobs = facility.job_queue(db, scheduler=cluster)
        return {"jobs": jobs, "count": len(jobs)}

    return await aggregate({"queue": _q})


@router.get("/job/{job_id}")
async def job(job_id: str, _=Depends(_viewer)) -> dict[str, Any]:
    """Per-job detail with resources, timing, and the MLflow cost link (F6 R2)."""
    detail = facility.job_detail(_platform_db_path(), job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return {"job": detail}


# ── fleet registry: clusters + approval gate (Phase 35b) ─────────────────────


class _RejectBody(BaseModel):
    reason: str | None = None


def _actor(claims: dict) -> str:
    return claims.get("role", "admin") if isinstance(claims, dict) else "admin"


@router.get("/fleet")
async def fleet(_=Depends(_viewer)) -> dict[str, Any]:
    """Registered clusters + approval state for the fleet panel."""
    clusters = facility.fleet_clusters(_platform_db_path())
    return {"clusters": clusters, "count": len(clusters)}


@router.post("/fleet/{name}/approve")
async def approve_cluster(name: str, claims: dict = Depends(_admin)) -> dict[str, Any]:
    """Sysadmin: approve a cluster so jobs may be scheduled on it (admin only, audited)."""
    ok = facility.set_cluster_state(_platform_db_path(), name, "ACTIVE", actor=_actor(claims))
    if not ok:
        raise HTTPException(status_code=404, detail=f"cluster {name} not found")
    return {"name": name, "state": "ACTIVE"}


@router.post("/fleet/{name}/reject")
async def reject_cluster(
    name: str, body: _RejectBody, claims: dict = Depends(_admin)
) -> dict[str, Any]:
    """Sysadmin: reject a cluster (blocks scheduling; admin only, audited)."""
    ok = facility.set_cluster_state(
        _platform_db_path(), name, "REJECTED", actor=_actor(claims), reason=body.reason
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"cluster {name} not found")
    return {"name": name, "state": "REJECTED", "reason": body.reason}

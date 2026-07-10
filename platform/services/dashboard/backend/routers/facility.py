"""Exascale facility-console BFF endpoints (F6 / ADR 0059).

Scheduler-neutral, view-shaped ``/api/v1/facility/*`` endpoints over the ``hpc_jobs`` table
(phase-23 scheduler abstraction). Composed through the F8 BFF substrate (per-source timeout +
``_partial`` fallback), viewer-gated, and graceful on missing telemetry (F6 R7). A ``?cluster=``
query rescopes every list for the multi-cluster switcher (F6 R6).
"""

from __future__ import annotations

import os
from typing import Any

import facility
from auth import require_role
from bff import aggregate
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/v1/facility", tags=["facility"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/overview")
async def overview(cluster: str | None = None, _=Depends(_viewer)) -> dict[str, Any]:
    """Facility KPIs + per-partition utilization (F6 R1)."""
    db = _platform_db_path()

    async def _ov() -> dict[str, Any]:
        return facility.facility_overview(db, scheduler=cluster)

    return await aggregate({"facility": _ov})


@router.get("/queue")
async def queue(cluster: str | None = None, _=Depends(_viewer)) -> dict[str, Any]:
    """Waiting jobs, longest-waiting first (F6 R2)."""
    db = _platform_db_path()

    async def _q() -> dict[str, Any]:
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

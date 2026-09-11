"""MLOps-console BFF endpoints (F9 / ADR 0060).

View-shaped ``/api/v1/mlops/*`` endpoints that surface the shipped MLOps backend (registry,
drift, cost, traffic, promotion policy) for the dashboard's MLOps console. Each endpoint
composes its sources through :func:`bff.aggregate`, inheriting the F8 per-source-timeout +
``_partial`` fallback, and is viewer-gated (authz enforced in the BFF, F9/F15).
"""

from __future__ import annotations

from typing import Any

import mlops
from auth import require_role
from bff import aggregate
from dbconn import platform_db_path
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/v1/mlops", tags=["mlops"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return platform_db_path()


@router.get("/registry")
async def registry(_=Depends(_viewer)) -> dict[str, Any]:
    """Registry grid: models × latest-version × stage with health/freshness (F9 R1)."""
    db = _platform_db_path()

    def _rows() -> dict[str, Any]:
        rows = mlops.registry_rows(db)
        return {"rows": rows, "count": len(rows)}

    return await aggregate({"registry": _rows})


@router.get("/model/{name}")
async def model_detail(name: str, _=Depends(_viewer)) -> dict[str, Any]:
    """Model detail 2.0 tabs: cost / drift / traffic / promotion (F9 R2)."""
    db = _platform_db_path()

    def _detail() -> dict[str, Any]:
        return mlops.model_detail(db, name)

    return await aggregate({"detail": _detail})


@router.get("/promotion/{name}")
async def promotion(name: str, _=Depends(_viewer)) -> dict[str, Any]:
    """Guided-promotion check: policy + eval + approval, denied with reasons (F9 R4)."""
    db = _platform_db_path()

    def _check() -> dict[str, Any]:
        return mlops.promotion_check(db, name)

    return await aggregate({"promotion": _check})


@router.get("/gate-reports/{name}")
async def gate_reports(name: str, limit: int = 20, _=Depends(_viewer)) -> dict[str, Any]:
    """The model's persisted eval-gate reports, newest first (ADR 0008 clause 4)."""
    db = _platform_db_path()

    def _reports() -> list[dict[str, Any]]:
        return mlops.gate_reports(db, name, limit)

    return await aggregate({"reports": _reports})

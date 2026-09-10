"""FinOps + Green-AI BFF endpoint (F13 / ADR 0066).

`GET /api/v1/finops/overview` composes cost rollups, budget-vs-actual, carbon accounting, and unit
economics through the F8 BFF substrate (per-source timeout + `_partial` fallback), viewer-gated.
Surfaces the shipped phase 23/24 cost/carbon backend.
"""

from __future__ import annotations

from typing import Any

import finops
from auth import require_role
from bff import aggregate
from dbconn import platform_db_path
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/v1/finops", tags=["finops"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return platform_db_path()


@router.get("/overview")
async def overview(_=Depends(_viewer)) -> dict[str, Any]:
    """FinOps + Green-AI overview: cost / budget / carbon / unit-economics (F13 R1–R4)."""
    db = _platform_db_path()

    return await aggregate(
        {
            "cost": lambda: _wrap(finops.cost_rollup, db),
            "budget": lambda: _wrap(finops.budget_status, db),
            "carbon": lambda: _wrap(finops.carbon_summary, db),
            "unitEconomics": lambda: _wrap(finops.unit_economics, db),
        }
    )


async def _wrap(fn, db: str) -> Any:
    """Adapt a sync aggregator into the async source signature `aggregate` expects."""
    return fn(db)

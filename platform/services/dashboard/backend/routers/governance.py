"""Governance & compliance BFF endpoint (F14 / ADR 0063).

`GET /api/v1/governance/overview` composes NIST posture, EU-AI-Act compliance, model-card coverage,
and audit-chain integrity through the F8 BFF substrate. Viewer-gated, partial-failure safe.
"""

from __future__ import annotations

import os
from typing import Any

import governance
from auth import require_role
from bff import aggregate
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/v1/governance", tags=["governance"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/overview")
async def overview(_=Depends(_viewer)) -> dict[str, Any]:
    """Governance overview: posture / compliance / cards / audit integrity (F14 R1–R5)."""
    db = _platform_db_path()

    async def _wrap(fn) -> Any:
        return fn(db)

    return await aggregate(
        {
            "posture": lambda: _wrap(governance.nist_posture),
            "compliance": lambda: _wrap(governance.compliance_status),
            "cards": lambda: _wrap(governance.model_card_coverage),
            "audit": lambda: _wrap(governance.audit_integrity),
        }
    )

"""Dashboard self-observability endpoints (F24 / ADR 0067).

`GET /api/v1/selfobs/status` powers the in-app status page (dependency health + self-metrics, R5).
`POST /api/v1/selfobs/action` audits a UI action to `platform_db` (R4 / D4). Viewer-gated.
"""

from __future__ import annotations

from typing import Any

import selfobs
from auth import require_role
from fastapi import APIRouter, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/v1/selfobs", tags=["selfobs"])
_viewer = require_role("viewer")


@router.get("/status")
async def status(_=Depends(_viewer)) -> dict[str, Any]:
    """In-app status page: dependency health + dashboard self-metrics (F24 R5)."""
    return selfobs.status_payload()


class UiAction(BaseModel):
    action: str
    target: str = ""
    details: str = ""


@router.post("/action")
async def ui_action(body: UiAction, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    """Audit a UI action to platform_db (F24 R4 / D4). The actor is the caller's role."""
    audited = selfobs.record_ui_action(
        action=body.action,
        target=body.target,
        actor=claims.get("role", "unknown"),
        details=body.details,
    )
    return {"audited": audited}

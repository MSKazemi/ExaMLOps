"""Feature-flag BFF endpoints (F25 / ADR 0070).

`GET /api/v1/flags` delivers server-evaluated flag **decisions** for the caller's context (R2).
`GET /api/v1/flags/admin` + `POST /api/v1/flags/{name}` are the audited admin surface (R4): the POST
persists an override, audits it, and publishes `event.flag_changed` on the F8 bus for live kill-switch
delivery.
"""

from __future__ import annotations

from typing import Any

import feature_flags
from auth import require_role
from dbconn import platform_db_path
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from realtime import bus

router = APIRouter(prefix="/v1/flags", tags=["flags"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _platform_db_path() -> str:
    return platform_db_path()


@router.get("")
async def flags(claims: dict = Depends(_viewer)) -> dict[str, Any]:
    """Server-evaluated flag decisions for the caller's context (F25 R2)."""
    role = claims.get("role", "")
    tenant = claims.get("tenant", "default")
    subject = claims.get("sub", role or "anonymous")
    return {
        "flags": feature_flags.evaluate_all(
            _platform_db_path(), role=role, tenant=tenant, subject=subject
        )
    }


@router.get("/admin")
async def admin(_=Depends(_admin)) -> dict[str, Any]:
    """Flag definitions + overrides + effective state for the admin UI (F25 R4)."""
    return feature_flags.admin_view(_platform_db_path())


class FlagSet(BaseModel):
    enabled: bool


@router.post("/{name}")
async def set_flag(name: str, body: FlagSet, claims: dict = Depends(_admin)) -> dict[str, Any]:
    """Set an admin override; audited + published for live kill-switch delivery (F25 R4/R2)."""
    actor = claims.get("role", "admin")
    ok = feature_flags.set_override(_platform_db_path(), name, body.enabled, actor)
    if ok:
        bus.publish("event.flag_changed", {"name": name, "enabled": body.enabled})
    return {"updated": ok, "name": name, "enabled": body.enabled}

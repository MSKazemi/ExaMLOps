"""Alerting BFF endpoints (F12 / ADR 0062).

`GET /api/v1/alerts` returns the unified alert inbox (drift + budget + eval). `POST
/api/v1/alerts/{id}/ack` acknowledges an alert — audited to `platform_db` (D4) and published on the
F8 `alert.*` channel so open dashboards update live. Viewer-gated, BFF-composed.
"""

from __future__ import annotations

from typing import Any

import alerts as alerts_lib
from auth import require_role
from bff import aggregate
from dbconn import platform_db_path
from fastapi import APIRouter, Depends
from realtime import bus

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return platform_db_path()


@router.get("")
async def list_alerts(_=Depends(_viewer)) -> dict[str, Any]:
    """Unified alert inbox across sources, severity-sorted (F12 R1)."""
    db = _platform_db_path()

    def _run() -> dict[str, Any]:
        return alerts_lib.active_alerts(db)

    return await aggregate({"inbox": _run})


@router.post("/{alert_id}/ack")
async def ack(alert_id: str, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    """Acknowledge an alert: audit to platform_db + publish on the F8 alert channel (F12 R3)."""
    actor = claims.get("role", "unknown")
    audited = alerts_lib.acknowledge(_platform_db_path(), alert_id, actor)
    bus.publish("alert.acked", {"id": alert_id, "actor": actor})
    return {"acked": True, "audited": audited}

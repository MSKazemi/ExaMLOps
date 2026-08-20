"""Self-driving autopilot kill-switch (ADR 0085, dashboard-rebuild M3).

Reads (viewer): whether the autopilot loop is enabled + recent run history. Writes (admin +
`autopilot.manage`, audited): flip the persistent kill-switch on/off — reusing the shared
`examlops.data.autopilot.set_autopilot_config` code path (pure platform.db). Mirrors
`exa autopilot enable|disable|status`. NB: `EXAMLOPS_AUTOPILOT_ENABLED`, if set, overrides this
persistent config at runtime (surfaced as `envOverride`). Running a cycle needs retrain/promote
infra → not exposed here.
"""

from __future__ import annotations

import json
import os

from auth import require_role
from capabilities import AUTOPILOT_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/autopilot", tags=["autopilot"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, AUTOPILOT_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, AUTOPILOT_MANAGE))


def _audit(conn, actor: str, action: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, "autopilot", json.dumps(details)),
    )


def _examlops_autopilot():
    """Lazy, guarded import of the shared autopilot config code path (503 if unavailable)."""
    try:
        from examlops.data import autopilot as _a  # type: ignore

        return _a
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "autopilot control requires the examlops package (not available in this deployment)",
        ) from exc


def _env_override() -> bool | None:
    """The runtime env override, if set (truthy/falsy), else None."""
    raw = os.getenv("EXAMLOPS_AUTOPILOT_ENABLED")
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


@router.get("/status")
async def autopilot_status(_=Depends(_viewer)) -> dict:
    """Persistent kill-switch state + recent run history (fail-open)."""
    enabled = False
    recent: list[dict] = []
    try:
        conn = connect(_db_path())
        try:
            row = conn.execute("SELECT value FROM autopilot_config WHERE key='enabled'").fetchone()
            enabled = bool(row) and str(row["value"]) == "1"
            recent = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, run_at, triggered_by, model_filter, dry_run, retrains_triggered, "
                    "promotions_made, policy_blocks, human_required, skipped, summary "
                    "FROM autopilot_runs ORDER BY id DESC LIMIT 10"
                ).fetchall()
            ]
            conn.close()
        finally:
            conn.close()
    except Exception:
        pass
    override = _env_override()
    return {
        "enabled": enabled,
        "envOverride": override,  # null unless EXAMLOPS_AUTOPILOT_ENABLED is set (overrides at runtime)
        "effective": override if override is not None else enabled,
        "recentRuns": recent,
    }


async def _set_enabled(value: str, action: str, principal: dict) -> dict:
    _require_manage(principal)
    a = _examlops_autopilot()
    a.set_autopilot_config("enabled", value)
    conn = connect(_db_path())
    try:
        _audit(conn, principal.get("sub", "?"), action, {"enabled": value == "1"})
        conn.commit()
        conn.close()
        return {"enabled": value == "1"}
    finally:
        conn.close()


@router.post("/enable")
async def enable(principal: dict = Depends(_admin)) -> dict:
    """Enable the autopilot kill-switch (admin; audited). Mirrors ``exa autopilot enable``."""
    return await _set_enabled("1", "autopilot_enabled", principal)


@router.post("/disable")
async def disable(principal: dict = Depends(_admin)) -> dict:
    """Disable the autopilot kill-switch (admin; audited). Mirrors ``exa autopilot disable``."""
    return await _set_enabled("0", "autopilot_disabled", principal)

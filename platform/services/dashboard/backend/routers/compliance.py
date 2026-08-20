"""EU AI Act compliance edit-parity (ADR 0012, dashboard-rebuild M1).

Reads (viewer): the `compliance_systems` register — the SAME table the `exa compliance` CLI writes
(the legacy Governance overview reads a different `compliance_records` table; this router deliberately
surfaces `compliance_systems` so dashboard-set classifications are visible). Writes (admin +
`compliance.classify`, audited): classify a system's EU-AI-Act risk tier and advance its conformity
state — reusing `examlops.compliance.classify_system` / `set_conformity_state` (which validate the
risk-tier vocabulary + the conformity state-machine and hash-chain the audit with `source=dashboard`),
so the dashboard can't drift from the CLI.
"""

from __future__ import annotations

import os

from auth import require_role
from capabilities import COMPLIANCE_CLASSIFY, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/compliance", tags=["compliance"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, COMPLIANCE_CLASSIFY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, COMPLIANCE_CLASSIFY))


def _examlops_compliance():
    """Lazy, guarded import of the shared CLI compliance code path (503 if unavailable)."""
    try:
        from examlops import compliance as _c  # type: ignore

        return _c
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "compliance writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("/systems")
async def list_systems(_=Depends(_viewer)) -> list[dict]:
    """EU-AI-Act system register (from `compliance_systems`, the table the CLI writes)."""
    try:
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT model, tenant, in_scope, risk_tier, intended_purpose, deployment_context, "
                "conformity_state, updated_at, updated_by FROM compliance_systems ORDER BY model"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


@router.get("/risk-tiers")
async def risk_tiers(_=Depends(_viewer)) -> dict:
    """The valid EU-AI-Act risk tiers + conformity states (for the UI's selects)."""
    c = _examlops_compliance()
    return {"riskTiers": list(c.RISK_TIERS), "conformityStates": list(c.CONFORMITY_STATES)}


@router.post("/classify/{model}")
async def classify(
    model: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Set a system's EU-AI-Act risk tier + intended purpose (admin; audited).

    Body: ``{riskTier, intendedPurpose?, deploymentContext?}``. Mirrors ``exa compliance classify``
    via the shared ``classify_system`` (which validates ``riskTier`` and audits ``source=dashboard``).
    """
    _require_manage(principal)
    risk_tier = (payload.get("riskTier") or "").strip()
    intended = (payload.get("intendedPurpose") or "").strip()
    context = (payload.get("deploymentContext") or "").strip()
    c = _examlops_compliance()
    try:
        c.classify_system(
            model, risk_tier, intended, context, principal.get("sub", "?"), source="dashboard"
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"model": model, "riskTier": risk_tier, "intendedPurpose": intended}


@router.post("/conformity/{model}")
async def set_conformity(
    model: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Advance a system's conformity state (admin; audited).

    Body: ``{state}`` ∈ draft|documented|assessed|declared. Mirrors ``exa compliance declare`` via the
    shared ``set_conformity_state`` (transition-validated; audits ``source=dashboard``). Invalid
    transitions → 400.
    """
    _require_manage(principal)
    state = (payload.get("state") or "").strip()
    c = _examlops_compliance()
    try:
        c.set_conformity_state(model, state, principal.get("sub", "?"), source="dashboard")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"model": model, "state": state}

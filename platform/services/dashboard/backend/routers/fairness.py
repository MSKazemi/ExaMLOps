"""Fairness configuration (C8, dashboard-rebuild M5).

Reads (viewer): per-model fairness configs (slicing attributes + disparity threshold). Writes (admin
+ `fairness.manage`, audited): declare a model's slicing attributes + threshold — reusing the shared
`examlops.data.governance.set_fairness_config` code path (pure platform.db). Mirrors
`exa fairness config`. A gated fairness config can block promotion when a slice breaches the threshold.
"""

from __future__ import annotations

import json

import audit_write
from auth import require_role
from capabilities import FAIRNESS_MANAGE, can, deny_reason, require_capability, scope_to_tenant
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, status
from readfail import readable

router = APIRouter(prefix="/fairness", tags=["fairness"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return platform_db_path()


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, FAIRNESS_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, FAIRNESS_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_governance():
    """Lazy, guarded import of the shared fairness config code path (503 if unavailable)."""
    try:
        from examlops.data import governance as _g  # type: ignore

        return _g
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "fairness writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def list_fairness(principal: dict = Depends(_viewer)) -> list[dict]:
    """Per-model fairness configs (slice_attrs parsed from JSON).

    A failed read is a 503, not an empty list: "no fairness policy is configured" is a claim about
    this centre's governance, and it must not be produced by an unreachable datastore.

    Scoped to the caller's tenant (F15 R4): the protected attributes a centre slices on are
    disclosive of its data, and its thresholds are its own policy.
    """
    with readable("the fairness policy register"):
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT model, tenant, slice_attrs, threshold, min_samples, gate_promotion, enabled, "
                "updated_at FROM fairness_config ORDER BY model"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["slice_attrs"] = json.loads(d.pop("slice_attrs") or "[]")
                d["gate_promotion"] = bool(d["gate_promotion"])
                d["enabled"] = bool(d["enabled"])
                out.append(d)
            configs = scope_to_tenant(principal, out)
        finally:
            conn.close()
    return configs


@router.post("", status_code=status.HTTP_201_CREATED)
async def set_fairness(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(FAIRNESS_MANAGE)),
) -> dict:
    """Declare a model's fairness config (admin; audited).

    Body: ``{model, sliceAttrs: string[], threshold?, minSamples?, gatePromotion?, enabled?}``.
    Mirrors ``exa fairness config`` via the shared `set_fairness_config`.
    """
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    slice_attrs = payload.get("sliceAttrs")
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")
    if not isinstance(slice_attrs, list) or not slice_attrs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sliceAttrs must be a non-empty list")
    try:
        threshold = float(payload.get("threshold", 0.1))
        min_samples = int(payload.get("minSamples", 30))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "threshold/minSamples must be numeric"
        ) from exc
    if not 0.0 <= threshold <= 1.0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "threshold must be in [0, 1]")
    gate = bool(payload.get("gatePromotion", False))
    enabled = bool(payload.get("enabled", True))
    gov = _examlops_governance()
    gov.set_fairness_config(
        model,
        [str(a) for a in slice_attrs],
        threshold=threshold,
        min_samples=min_samples,
        gate_promotion=gate,
        enabled=enabled,
    )
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "fairness_config_set",
            model,
            {"slice_attrs": slice_attrs, "threshold": threshold, "gate_promotion": gate},
        )
        conn.commit()
        conn.close()
        return {
            "model": model,
            "sliceAttrs": slice_attrs,
            "threshold": threshold,
            "gatePromotion": gate,
            "enabled": enabled,
        }
    finally:
        conn.close()

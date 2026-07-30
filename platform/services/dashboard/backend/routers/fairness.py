"""Fairness configuration (C8, dashboard-rebuild M5).

Reads (viewer): per-model fairness configs (slicing attributes + disparity threshold). Writes (admin
+ `fairness.manage`, audited): declare a model's slicing attributes + threshold — reusing the shared
`examlops.data.governance.set_fairness_config` code path (pure platform.db). Mirrors
`exa fairness config`. A gated fairness config can block promotion when a slice breaches the threshold.
"""

from __future__ import annotations

import json
import os

from auth import require_role
from capabilities import FAIRNESS_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/fairness", tags=["fairness"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, FAIRNESS_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, FAIRNESS_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


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
async def list_fairness(_=Depends(_viewer)) -> list[dict]:
    """Per-model fairness configs (slice_attrs parsed from JSON). Fail-open to []."""
    try:
        conn = connect(_db_path())
        rows = conn.execute(
            "SELECT model, tenant, slice_attrs, threshold, min_samples, gate_promotion, enabled, "
            "updated_at FROM fairness_config ORDER BY model"
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["slice_attrs"] = json.loads(d.pop("slice_attrs") or "[]")
            d["gate_promotion"] = bool(d["gate_promotion"])
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out
    except Exception:
        return []


@router.post("", status_code=status.HTTP_201_CREATED)
async def set_fairness(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
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

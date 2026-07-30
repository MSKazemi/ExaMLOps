"""Model-quality SLOs (C6 / ADR 0023, dashboard-rebuild M3).

Reads (viewer): the `slo_specs` register + best-effort live status (SLI / remaining error budget /
burn rate). Writes (admin + `slo.manage`, audited): define/update an SLO spec — reusing the shared
`examlops.slo.apply_spec` → `upsert_slo_spec` code path (pure platform.db). Mirrors `exa slo set`.
"""

from __future__ import annotations

import json
import os

from auth import require_role
from capabilities import SLO_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/slo", tags=["slo"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, SLO_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, SLO_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


def _examlops_slo():
    """Lazy, guarded import of the shared SLO code path (503 if unavailable)."""
    try:
        from examlops import slo as _s  # type: ignore

        return _s
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SLO writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def list_slos(_=Depends(_viewer)) -> list[dict]:
    """SLO specs + best-effort live status (SLI / budget-remaining / burn-rate). Fail-open to []."""
    try:
        conn = connect(_db_path())
        rows = conn.execute(
            "SELECT model, tenant, name, sli_source, sli_query, target, window, higher_is_better, "
            "version, gate_promotion, updated_at FROM slo_specs ORDER BY model, name"
        ).fetchall()
        conn.close()
        specs = [dict(r) for r in rows]
    except Exception:
        return []
    # Best-effort live status via the shared computation; never fail the list if it's unavailable.
    status_by_key: dict[tuple[str, str, str], dict] = {}
    try:
        from examlops import slo as _s  # type: ignore

        for model in {s["model"] for s in specs}:
            for st in _s.slo_status(model):
                status_by_key[(st.model, st.tenant, st.name)] = {
                    "sli": round(st.sli, 4),
                    "budgetRemaining": round(st.budget_remaining, 4),
                    "burnRate": (None if st.burn_rate == float("inf") else round(st.burn_rate, 3)),
                    "ok": st.ok,
                    "n": st.n,
                }
    except Exception:
        status_by_key = {}
    for s in specs:
        s["higher_is_better"] = bool(s["higher_is_better"])
        s["gate_promotion"] = bool(s["gate_promotion"])
        s["status"] = status_by_key.get((s["model"], s["tenant"], s["name"]))
    return specs


@router.post("")
async def set_slo(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Define/update an SLO spec (admin; audited).

    Body: ``{model, name, target, sliSource?, sliQuery?, window?, higherIsBetter?, gatePromotion?}``.
    Mirrors ``exa slo set`` via the shared `examlops.slo.apply_spec`.
    """
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    name = (payload.get("name") or "").strip()
    if not model or not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model and name are required")
    try:
        target = float(payload.get("target"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "target must be a number") from exc
    if not 0.0 < target <= 1.0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "target must be in (0, 1]")
    spec = {
        "model": model,
        "name": name,
        "tenant": (payload.get("tenant") or "default").strip() or "default",
        "sli_source": (payload.get("sliSource") or "prometheus").strip() or "prometheus",
        "sli_query": payload.get("sliQuery"),
        "target": target,
        "window": (payload.get("window") or "30d").strip() or "30d",
        "higher_is_better": bool(payload.get("higherIsBetter", True)),
        "gate_promotion": bool(payload.get("gatePromotion", False)),
    }
    s = _examlops_slo()
    s.apply_spec(spec)
    conn = connect(_db_path())
    _audit(
        conn,
        principal.get("sub", "?"),
        "slo_set",
        model,
        {"name": name, "target": target, "gate_promotion": spec["gate_promotion"]},
    )
    conn.commit()
    conn.close()
    return {"model": model, "name": name, "target": target, "gatePromotion": spec["gate_promotion"]}

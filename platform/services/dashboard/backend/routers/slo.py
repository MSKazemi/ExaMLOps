"""Model-quality SLOs (C6 / ADR 0023, dashboard-rebuild M3).

Reads (viewer): the `slo_specs` register + best-effort live status (SLI / remaining error budget /
burn rate). Writes (admin + `slo.manage`, audited): define/update an SLO spec — reusing the shared
`examlops.slo.apply_spec` → `upsert_slo_spec` code path (pure platform.db). Mirrors `exa slo set`.
"""

from __future__ import annotations

import audit_write
from auth import require_role
from capabilities import SLO_MANAGE, can, deny_reason, require_capability, scope_to_tenant
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, status
from readfail import readable

router = APIRouter(prefix="/slo", tags=["slo"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return platform_db_path()


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, SLO_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, SLO_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


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
async def list_slos(principal: dict = Depends(_viewer)) -> list[dict]:
    """SLO specs + best-effort live status (SLI / budget-remaining / burn-rate). Fail-open to [].

    Scoped to the caller's tenant (F15 R4). The principal used to be bound to ``_`` and the query
    had no ``WHERE`` at all, so every viewer of every tenant saw every other tenant's SLO
    definitions — ``sli_query`` included, which is a Prometheus expression carrying that tenant's
    metric and label names.
    """
    with readable("the SLO register"):
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT model, tenant, name, sli_source, sli_query, target, window, higher_is_better, "
                "version, gate_promotion, updated_at FROM slo_specs ORDER BY model, name"
            ).fetchall()
            specs = scope_to_tenant(principal, [dict(r) for r in rows])
        finally:
            conn.close()
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
                    # Without this the console cannot tell an SLO meeting its target from one
                    # nobody has measured: zero samples score a perfect SLI, so the row rendered
                    # a green "Meeting" pill reading "SLI 100.00% · budget 100%".
                    "measured": st.measured,
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
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(SLO_MANAGE)),
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
    # A missing key used to arrive here as None and be rejected by the TypeError below. That
    # worked, but by accident; rejecting it up front gives the same 400 with the same message.
    raw_target = payload.get("target")
    if raw_target is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "target must be a number")
    try:
        target = float(raw_target)
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
    # Returned to the caller rather than logged: whoever wrote the query is the one who can fix it.
    spec_warnings = s.apply_spec(spec) or []
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "slo_set",
            model,
            {"name": name, "target": target, "gate_promotion": spec["gate_promotion"]},
        )
        conn.commit()
        conn.close()
        return {
            "model": model,
            "name": name,
            "target": target,
            "gatePromotion": spec["gate_promotion"],
            # Advisory, never a refusal: the spec above is already written. The console shows these
            # so the person who wrote the query learns what the CLI would have told them.
            "warnings": spec_warnings,
        }
    finally:
        conn.close()

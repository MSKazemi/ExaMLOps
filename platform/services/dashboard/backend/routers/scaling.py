"""Scaling & Routing console (E4/E5 · ADR 0031/0039, dashboard-enterprise-rebuild · Serve group).

Surfaces the `exa serve autoscale` and `exa serve routing` CLI capabilities in the dashboard.

Reads (viewer): a model's autoscale policy/config + recent scale events + scale-to-zero savings,
and its inference-routing config + recorded routing stats. Writes (admin + `scaling.manage`,
audited `source=dashboard`): set the autoscale policy — reusing the shared
`examlops.autoscale.set_policy` code path (mirrors `exa serve autoscale set`) — and set the routing
config via `examlops.data.gateway.set_gateway_config` (mirrors `exa serve routing set`).

Pure `platform.db` — config/policy/recorded-stats only; no live Ray runtime required (actuation is
out of scope).
"""

from __future__ import annotations

import audit_write
from auth import require_role
from capabilities import SCALING_MANAGE, can, deny_reason, require_capability
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/scaling", tags=["scaling"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return platform_db_path()


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, SCALING_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, SCALING_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_autoscale():
    """Lazy, guarded import of the shared autoscale code paths (503 if unavailable)."""
    try:
        from examlops import autoscale as _a  # type: ignore
        from examlops.data import serving as _s  # type: ignore

        return _a, _s
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scaling writes require the examlops package (not available in this deployment)",
        ) from exc


def _examlops_routing():
    """Lazy, guarded import of the shared routing code paths (503 if unavailable)."""
    try:
        from examlops.data import events as _e  # type: ignore
        from examlops.data import gateway as _g  # type: ignore

        return _g, _e
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "routing writes require the examlops package (not available in this deployment)",
        ) from exc


# ── autoscale ─────────────────────────────────────────────────────────────────


@router.get("/autoscale")
async def get_autoscale(
    model: str = Query(..., description="Model name"),
    _=Depends(_viewer),
) -> dict:
    """Autoscale policy/config + recent scale events + scale-to-zero savings. Mirrors `exa serve
    autoscale status/savings`. Fail-open: a missing policy returns ``config: null`` (never a 500)."""
    autoscale, serving = _examlops_autoscale()
    try:
        cfg = serving.get_autoscale_config(model)
        events = serving.list_scale_events(model, last_n=10) if cfg else []
        savings = autoscale.scale_to_zero_savings(model) if cfg else None
    except Exception:
        return {"model": model, "config": None, "events": [], "savings": None}
    return {"model": model, "config": cfg, "events": events, "savings": savings}


@router.post("/autoscale")
async def set_autoscale(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(SCALING_MANAGE)),
) -> dict:
    """Declare/patch a model's autoscale policy (admin; audited `source=dashboard`).

    Body: ``{model, minReplicas?, maxReplicas?, targetMetric?, targetValue?, scaleToZeroAfterS?,
    warmPool?, gpuFraction?, tenant?}``. Mirrors ``exa serve autoscale set`` via the shared
    ``examlops.autoscale.set_policy``.
    """
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")
    try:
        min_replicas = int(payload.get("minReplicas", 1))
        max_replicas = int(payload.get("maxReplicas", 4))
        target_value = float(payload.get("targetValue", 10.0))
        scale_to_zero_after_s = int(payload.get("scaleToZeroAfterS", 0))
        warm_pool = int(payload.get("warmPool", 0))
        gpu_fraction = float(payload.get("gpuFraction", 1.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "numeric fields must be numbers") from exc
    if min_replicas < 0 or max_replicas < 1 or max_replicas < min_replicas:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "require 0 <= minReplicas <= maxReplicas and maxReplicas >= 1",
        )
    metric = (payload.get("targetMetric") or "queue_depth").strip() or "queue_depth"
    tenant = (payload.get("tenant") or "default").strip() or "default"

    autoscale, _serving = _examlops_autoscale()
    autoscale.set_policy(
        model,
        tenant=tenant,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        target_metric=metric,
        target_value=target_value,
        scale_to_zero_after_s=scale_to_zero_after_s,
        warm_pool=warm_pool,
        gpu_fraction=gpu_fraction,
    )
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "autoscale_policy_set",
            model,
            {
                "min_replicas": min_replicas,
                "max_replicas": max_replicas,
                "target_metric": metric,
                "target_value": target_value,
                "scale_to_zero_after_s": scale_to_zero_after_s,
            },
        )
        conn.commit()
        conn.close()
        return {
            "model": model,
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "targetMetric": metric,
            "targetValue": target_value,
            "scaleToZeroAfterS": scale_to_zero_after_s,
        }
    finally:
        conn.close()


# ── routing ───────────────────────────────────────────────────────────────────


@router.get("/routing")
async def get_routing(
    model: str = Query(..., description="Model name"),
    tenant: str = Query("default", description="Tenant scope"),
    _=Depends(_viewer),
) -> dict:
    """Inference-routing config + recorded routing stats (hit rate / decision breakdown). Mirrors
    `exa serve routing stats`. Fail-open: unreachable state returns ``config: null``."""
    gateway, events = _examlops_routing()
    try:
        cfg = gateway.get_gateway_config(model, tenant)
        stats = events.routing_stats(model, tenant)
    except Exception:
        return {"model": model, "config": None, "stats": None}
    return {"model": model, "config": cfg, "stats": stats}


@router.post("/routing")
async def set_routing(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(SCALING_MANAGE)),
) -> dict:
    """Configure a model's inference routing (admin; audited `source=dashboard`).

    Body: ``{model, mode?, sloLatencyMs?, disaggregate?, prefillPool?, decodePool?, tenant?}``.
    Mirrors ``exa serve routing set`` via the shared ``examlops.data.gateway.set_gateway_config``.
    """
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")
    mode = (payload.get("mode") or "round_robin").strip() or "round_robin"
    if mode not in ("round_robin", "cache_aware"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mode must be round_robin or cache_aware")
    slo = payload.get("sloLatencyMs")
    try:
        slo_latency_ms = float(slo) if slo is not None and slo != "" else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sloLatencyMs must be a number") from exc
    disaggregate = bool(payload.get("disaggregate", False))
    prefill_pool = (payload.get("prefillPool") or "").strip() or None
    decode_pool = (payload.get("decodePool") or "").strip() or None
    tenant = (payload.get("tenant") or "default").strip() or "default"

    gateway, _events = _examlops_routing()
    gateway.set_gateway_config(
        model,
        tenant=tenant,
        mode=mode,
        slo_latency_ms=slo_latency_ms,
        disaggregate=disaggregate,
        prefill_pool=prefill_pool,
        decode_pool=decode_pool,
    )
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "routing_config_set",
            model,
            {"mode": mode, "slo_latency_ms": slo_latency_ms, "disaggregate": disaggregate},
        )
        conn.commit()
        conn.close()
        return {
            "model": model,
            "mode": mode,
            "sloLatencyMs": slo_latency_ms,
            "disaggregate": disaggregate,
        }
    finally:
        conn.close()

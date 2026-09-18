"""Platform Ops console — the dashboard surface over ``examlops.platform_admin``.

The observe + act half of the platform-management story (the Jupyter workbench is the other half). It
unifies what was scattered across separate consoles — the compute-node **cost rate card**, authored
**providers**, and the platform-management **change feed** — into one operator view, and lets an admin
make the same governed changes from the browser.

Every write reuses the **exact same** ``examlops.platform_admin`` façade the workbench uses (via
``acting_as`` so the audit row is attributed to the logged-in principal, ``source=dashboard``), so the
dashboard can never drift from the notebook/CLI and every change is RBAC + policy + audit governed.
Reads are viewer-gated; writes require ``platform.manage`` (admin).
"""

from __future__ import annotations

from auth import require_role
from capabilities import PLATFORM_MANAGE, can, deny_reason, require_capability
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/platform-ops", tags=["platform-ops"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, PLATFORM_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, PLATFORM_MANAGE))


def _pa():
    """Lazy, guarded import of the shared façade (503 if the examlops package is unavailable)."""
    try:
        from examlops import platform_admin as pa  # type: ignore

        return pa
    except ImportError as exc:  # pragma: no cover - only when examlops is absent
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "platform-ops requires the examlops package (not available in this deployment)",
        ) from exc


def _governed(principal: dict, call):
    """Run a façade write as the principal (dashboard-attributed) and translate its errors to HTTP."""
    pa = _pa()
    with pa.acting_as(principal.get("sub", "dashboard"), source="dashboard"):
        try:
            return call(pa)
        except pa.PlatformAdminApprovalRequired as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except pa.PlatformAdminDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except pa.PlatformAdminError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except Exception as exc:
            # AST-gate rejections of authored provider code (ProviderSecurityError / ProviderError)
            # are bad *input*, not a server fault — surface as 400; re-raise anything else.
            if exc.__class__.__name__ in {"ProviderError", "ProviderSecurityError"}:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            raise


# ── reads (viewer) ────────────────────────────────────────────────────────────


@router.get("/overview")
async def overview(_=Depends(_viewer)) -> dict:
    """The unified Platform Ops read: cost rate card, authored providers, recent changes."""
    pa = _pa()
    return {
        "cost_card": pa.compute_cost_card(),
        "providers": pa.list_authored_providers(),
        "changes": pa.recent_changes(limit=25),
    }


@router.get("/cost-card")
async def cost_card(_=Depends(_viewer)) -> dict:
    """The effective compute-node cost rate card + provider methodology."""
    return _pa().compute_cost_card()


@router.get("/changes")
async def changes(limit: int = Query(50, ge=1, le=500), _=Depends(_viewer)) -> list[dict]:
    """The platform-management change feed (workbench + dashboard audit rows, newest first)."""
    return _pa().recent_changes(limit=limit)


@router.get("/providers")
async def providers(project: str = Query("platform-ops"), _=Depends(_viewer)) -> list[dict]:
    """Authored providers for a project (name/domain/active/gate-status)."""
    return _pa().list_authored_providers(project)


# ── writes (admin, platform.manage) ───────────────────────────────────────────


@router.post("/cost")
async def set_cost(
    body: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PLATFORM_MANAGE)),
) -> dict:
    """Set the compute-node cost rate card. Governed + audited (source=dashboard)."""
    _require_manage(principal)
    gpu = body.get("gpu_per_hour")
    cpu = body.get("cpu_per_hour")
    provider = body.get("provider")
    return _governed(
        principal,
        lambda pa: pa.set_compute_cost(
            gpu_per_hour=gpu, cpu_per_hour=cpu, provider=provider, approve=bool(body.get("approve"))
        ),
    )


@router.post("/provider")
async def deploy_provider(
    body: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PLATFORM_MANAGE)),
) -> dict:
    """Deploy (AST-gate + activate) a calculation provider authored in the browser. Governed + audited."""
    _require_manage(principal)
    for field in ("domain", "name", "code"):
        if not body.get(field):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"missing {field!r}")
    return _governed(
        principal,
        lambda pa: pa.deploy_provider(
            body["domain"],
            body["name"],
            body["code"],
            project=body.get("project", "platform-ops"),
            activate=bool(body.get("activate", True)),
            approve=bool(body.get("approve")),
        ),
    )


@router.post("/knob")
async def set_knob(
    body: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(PLATFORM_MANAGE)),
) -> dict:
    """Set a platform.db knob (traffic/autoscale) for a model. Governed + audited."""
    _require_manage(principal)
    for field in ("domain", "model", "params"):
        if body.get(field) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"missing {field!r}")
    return _governed(
        principal,
        lambda pa: pa.set_knob(
            body["domain"], body["model"], dict(body["params"]), approve=bool(body.get("approve"))
        ),
    )

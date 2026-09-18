"""Champion-challenger console (ADR 0024 clause 4).

The scoreboard, the Welch/z-test engine and `exa serve challenger` all existed; the dashboard had
only a read-only *shadow* router, so the comparison an operator is meant to act on — is the
challenger actually better, and is it safe to promote — was CLI-only.

Reads (viewer): configured challengers and, per model, the scored comparison (delta, p-value,
sample count, SLO verdict, whether the promotion policy is met). Writes: **disable** needs
`traffic.manage`, the capability that already governs enabling shadow traffic; **promote** needs
`model.promote`, which is a step-up capability — promotion is the consequential action here and it
already has an established gate, so this router reuses both rather than inventing a
`challenger.manage` that would be a third name for the same authority.

Every operation goes through `examlops.champion_challenger` / `examlops.data.serving` — the same
code paths the CLI uses — so the two surfaces cannot disagree, and the audit events are the ones
the CLI already writes.
"""

from __future__ import annotations

from auth import require_role
from capabilities import (
    MODEL_PROMOTE,
    TRAFFIC_MANAGE,
    can,
    deny_reason,
    require_capability,
    scope_to_tenant,
)
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/challenger", tags=["challenger"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _require(principal: dict, capability: str) -> None:
    role = principal.get("role", "")
    if not can(role, capability):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, capability))


def _actor(principal: dict) -> str:
    return str(principal.get("sub") or principal.get("role") or "unknown")


def _cc():
    """Lazy, guarded import of the shared champion-challenger code path (503 if unavailable)."""
    try:
        from examlops import champion_challenger as _cc  # type: ignore

        return _cc
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "challenger control requires the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def list_challengers(principal: dict = Depends(_viewer)) -> list[dict]:
    """Configured challengers for the caller's tenant. Empty when none exist or unreadable.

    `list_challenger_configs()` takes `tenant=None` to mean *every* tenant, and this asked for
    exactly that. A helper whose default is "no filter" reads like a helper with a safe
    default, which is why this kind of call slips past a guard that only scans raw SQL.
    """
    try:
        from examlops.data.serving import list_challenger_configs  # type: ignore

        return scope_to_tenant(principal, list(list_challenger_configs()))
    except Exception:
        return []


@router.get("/{model}")
async def challenger_status(model: str, _=Depends(_viewer)) -> dict:
    """The scored comparison for one model.

    ``configured: false`` rather than a 404 when no challenger exists: the console asks this for
    whichever model is selected, and "there is no challenger here" is an ordinary answer, not an
    error worth an error state in the UI.
    """
    st = _cc().challenger_status(model)
    if st is None:
        return {"model": model, "configured": False}
    return {**st.as_dict(), "configured": True}


@router.post("/{model}/promote")
async def promote(
    model: str,
    principal=Depends(_admin),
    # `model.promote` is a **step-up** capability (RFC 9470) and, for a federated user, one the
    # centre's own PDP may veto. Both live in `iam_gate.enforce`, which only `require_capability`
    # calls — so checking the capability with a bare `can()` reused the *name* of the gate without
    # the gate. With step-up opted in, `/api/models/…/promote` demanded re-authentication while
    # this door, to the same authority, did not. Kept alongside the admin dependency rather than
    # replacing it: `require_capability` admits operators too, and widening who may promote is not
    # this fix's business.
    _gate: dict = Depends(require_capability(MODEL_PROMOTE)),
) -> dict:
    """Propose promotion of the challenger (C3-gated, audited).

    Returns ``proposed: false`` with the current status when the policy is not met — the operator
    needs to see *why* it was refused (not significant / too few samples / SLO regression), and an
    error would throw that away.
    """
    _require(principal, MODEL_PROMOTE)
    proposal = _cc().maybe_promote(model, actor=_actor(principal))
    if proposal is None:
        st = _cc().challenger_status(model)
        return {
            "model": model,
            "proposed": False,
            "reason": "promotion policy not met",
            "status": st.as_dict() if st is not None else None,
        }
    return {"model": model, "proposed": True, **_proposal_dict(proposal)}


def _proposal_dict(p) -> dict:
    return {
        "challengerVersion": p.challenger_version,
        "delta": p.delta,
        "pValue": p.p_value,
        "n": p.n,
        "auto": p.auto,
        "reason": p.reason,
    }


@router.post("/{model}/disable")
async def disable(model: str, principal=Depends(_admin)) -> dict:
    """Stop the challenger for a model (audited)."""
    _require(principal, TRAFFIC_MANAGE)
    actor = _actor(principal)
    from examlops.champion_challenger import platform_db as _pdb  # type: ignore

    _pdb.disable_challenger(model, updated_by=actor)
    # Through `write_audit_event`, not a raw INSERT: `audit_events` is hash-chained (D4), and a
    # direct insert writes a row with no `prev_hash`/`hash`, leaving a gap in the chain that
    # `exa audit verify` reports as tampering. It also has to be committed by hand — the first
    # version of this did neither, and the audit assertion in the tests is what caught it.
    _pdb.write_audit_event("dashboard", actor, "challenger_disable", model, None)
    return {"model": model, "enabled": False}

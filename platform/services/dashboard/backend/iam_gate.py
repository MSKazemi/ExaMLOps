"""The dashboard's policy enforcement point for ADR 0120, after the capability check passed.

Two further gates, both opt-in per deployment and both no-ops by default:

1. **Step-up (RFC 9470).** For a capability in ``STEP_UP_CAPABILITIES`` the session must satisfy
   the step-up policy — the user's center's ``step_up`` entry in the trust file, or
   ``EXAMLOPS_IAM_STEP_UP=enforce`` for a local password session. Otherwise: ``401`` with
   ``WWW-Authenticate: Bearer error="insufficient_user_authentication", acr_values=…, max_age=…``;
   the SPA sends the user back through SSO with those parameters.
2. **The data center's PDP.** For a federated session whose center set ``authorization.mode`` to
   ``external`` or ``both``, ``examlops.iam.authorize`` asks the center's AuthZEN/OPA service about
   ``(user, capability, dashboard route)``. Deny-overrides; an unreachable PDP denies.

Local password sessions never reach the center's PDP: they are not the center's users.
"""

from __future__ import annotations

import logging
from typing import Any

from capabilities import can, requires_step_up
from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


def _iam():
    try:
        from examlops import iam  # type: ignore

        return iam
    except ImportError:
        return None


def provider_for(claims: dict[str, Any]):
    """The trust-file entry for a federated session, or ``None`` for a local session."""
    idp = claims.get("idp")
    iam = _iam()
    if not idp or iam is None:
        return None
    try:
        return iam.load_config().by_name(idp)
    except iam.IamConfigError:
        return None


def _step_up(capability: str, claims: dict[str, Any]) -> None:
    if not requires_step_up(capability):
        return
    try:
        from examlops.iam import stepup  # type: ignore
    except ImportError:
        return
    provider = provider_for(claims)
    policy = provider.step_up if provider is not None else stepup.local_policy()
    challenge = stepup.evaluate(policy, acr=claims.get("acr"), auth_time=claims.get("auth_time"))
    if challenge is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={**challenge.as_dict(), "provider": claims.get("idp")},
            headers={"WWW-Authenticate": challenge.www_authenticate()},
        )


def principal_from_session(claims: dict[str, Any]):
    """Rebuild an ``examlops.iam.Principal`` from federated session claims."""
    iam = _iam()
    if iam is None or not claims.get("idp"):
        return None
    return iam.Principal(
        provider=str(claims["idp"]),
        issuer=str(claims.get("iss_idp", "")),
        subject=str(claims.get("idp_sub") or claims.get("sub", "")),
        tenant=str(claims.get("tenant", "default")),
        role=claims.get("role"),
        username=str(claims.get("name") or ""),
        email=str(claims.get("email") or ""),
        groups=tuple(claims.get("groups") or ()),
        projects=dict(claims.get("projects") or {}),
        acr=claims.get("acr"),
        amr=tuple(claims.get("amr") or ()),
        auth_time=claims.get("auth_time"),
    )


def _center_pdp(capability: str, claims: dict[str, Any], request: Request | None) -> None:
    provider = provider_for(claims)
    if provider is None or provider.pdp is None or provider.authorization_mode == "local":
        return
    iam = _iam()
    principal = principal_from_session(claims)
    if iam is None or principal is None:
        return
    decision = iam.authorize(
        principal,
        capability,
        {
            "type": "dashboard",
            "id": request.url.path if request is not None else capability,
            "method": request.method if request is not None else "",
            "tenant": principal.tenant,
        },
        local_allowed=can(str(claims.get("role", "")), capability),
    )
    if not decision.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Denied by policy: {decision.reason}")


def center_route_check(claims: dict[str, Any], request: Request | None) -> None:
    """Every authenticated route, for a federated caller: may the center's PDP veto it?

    The action is ``api.read`` for safe methods and ``api.write`` otherwise — the same vocabulary
    the control plane sends — so one center policy governs both services.
    """
    provider = provider_for(claims)
    if provider is None or provider.pdp is None or provider.authorization_mode == "local":
        return
    iam = _iam()
    principal = principal_from_session(claims)
    if iam is None or principal is None:
        return
    method = request.method if request is not None else "GET"
    decision = iam.authorize(
        principal,
        "api.read" if method in {"GET", "HEAD", "OPTIONS"} else "api.write",
        {
            "type": "dashboard",
            "id": request.url.path if request is not None else "",
            "method": method,
            "tenant": principal.tenant,
        },
        local_allowed=True,  # the role check already passed
    )
    if not decision.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Denied by policy: {decision.reason}")


def enforce(capability: str, claims: dict[str, Any], request: Request | None) -> None:
    """Step-up, then the center's PDP. Raises ``HTTPException`` (401/403) or returns."""
    _step_up(capability, claims)
    _center_pdp(capability, claims, request)

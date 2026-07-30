"""Capability model + tenant scoping (F15 / ADR 0057).

The dashboard's 2-role JWT is mapped to a **capability set** so the UI can render authorized
affordances (and *explain* why a control is disabled) while the BFF stays the sole enforcement
point. This is the seam a later OIDC/OpenFGA migration slots into: swap `capabilities_for` for a
relationship check; the `require_capability` dependency and the `/me` capability list stay put.

Also provides **tenant scoping** helpers (F15 R4, default-deny): a principal only sees its own
tenant's resources unless it is a cross-tenant admin.
"""

from __future__ import annotations

from auth import require_role
from fastapi import Depends, HTTPException, status

# ── capability catalogue ──────────────────────────────────────────────────────

# Read affordances every authenticated user has.
VIEW = "view"
SEARCH = "search"
# Governed / mutating affordances (admin-only today).
MODEL_PROMOTE = "model.promote"
APPROVAL_DECIDE = "approval.decide"
RETRAIN_TRIGGER = "retrain.trigger"
DRIFT_BASELINE = "drift.baseline"
CONFIG_WRITE = "config.write"
SECRET_REVEAL = "secret.reveal"
SERVICE_CONTROL = "service.control"
PROJECT_MANAGE = "project.manage"  # ADR 0086 — create/assign/member/quota on a Project
CONNECTION_MANAGE = "connection.manage"  # ADR 0087 — create/delete/test named connections
COMPLIANCE_CLASSIFY = "compliance.classify"  # ADR 0012 — set EU-AI-Act risk tier / conformity state
GATEWAY_MANAGE = "gateway.manage"  # B2 gateway — issue / revoke virtual keys
PROVIDERS_MANAGE = "providers.manage"  # ADR 0074 — author/activate/delete calculation providers
PROMPT_MANAGE = "prompt.manage"  # B1 prompt registry — create version / set label / rollback
AUTOPILOT_MANAGE = (
    "autopilot.manage"  # ADR 0085 — enable/disable the self-driving autopilot kill-switch
)
SLO_MANAGE = "slo.manage"  # C6/ADR 0023 — define model-quality SLO specs
SECRETS_MANAGE = "secrets.manage"  # D7 — set/rotate platform secrets (values never returned)
FEATURE_MANAGE = "feature.manage"  # A3 feature store — register/patch feature views
FAIRNESS_MANAGE = "fairness.manage"  # C8 — configure fairness slicing + disparity thresholds
SCALING_MANAGE = "scaling.manage"  # E4/E5 — set autoscale policy + inference-routing config
ADMISSION_MANAGE = "admission.manage"  # item 1.5 — submit work to the fair-share admission queue
EVENTS_MANAGE = "events.manage"  # item 1.3 — publish an event to the NovaFabric outbox backbone
TRAFFIC_MANAGE = "traffic.manage"  # serve ab/shadow — start/stop A/B tests + enable/disable shadow
PLATFORM_MANAGE = (
    "platform.manage"  # Platform Ops — cost/provider/knob writes via examlops.platform_admin
)

# Actions that additionally require step-up/MFA (F15 R6 / F16). Enforcement is deferred; the flag
# is surfaced so the UI can prompt and the audit trail can record it.
STEP_UP_CAPABILITIES: frozenset[str] = frozenset({MODEL_PROMOTE, SECRET_REVEAL})

_VIEWER_CAPS: frozenset[str] = frozenset({VIEW, SEARCH})
_ADMIN_CAPS: frozenset[str] = _VIEWER_CAPS | frozenset(
    {
        MODEL_PROMOTE,
        APPROVAL_DECIDE,
        RETRAIN_TRIGGER,
        DRIFT_BASELINE,
        CONFIG_WRITE,
        SECRET_REVEAL,
        SERVICE_CONTROL,
        PROJECT_MANAGE,
        CONNECTION_MANAGE,
        COMPLIANCE_CLASSIFY,
        GATEWAY_MANAGE,
        PROVIDERS_MANAGE,
        PROMPT_MANAGE,
        AUTOPILOT_MANAGE,
        SLO_MANAGE,
        SECRETS_MANAGE,
        FEATURE_MANAGE,
        FAIRNESS_MANAGE,
        SCALING_MANAGE,
        ADMISSION_MANAGE,
        EVENTS_MANAGE,
        TRAFFIC_MANAGE,
        PLATFORM_MANAGE,
    }
)

_CAPS_BY_ROLE: dict[str, frozenset[str]] = {
    "viewer": _VIEWER_CAPS,
    "admin": _ADMIN_CAPS,
}


def capabilities_for(role: str) -> list[str]:
    """The sorted capability list for a role (unknown role ⇒ none, default-deny)."""
    return sorted(_CAPS_BY_ROLE.get(role, frozenset()))


def can(role: str, capability: str) -> bool:
    """Whether ``role`` holds ``capability``."""
    return capability in _CAPS_BY_ROLE.get(role, frozenset())


def deny_reason(role: str, capability: str) -> str:
    """Human explanation for a denied capability (F15 R3 — never a silent dead control)."""
    if can(role, capability):
        return ""
    if capability in _ADMIN_CAPS and role == "viewer":
        return "Requires the admin role."
    return f"Your role ('{role}') does not permit '{capability}'."


def requires_step_up(capability: str) -> bool:
    """Whether a capability needs step-up/MFA before the BFF permits it (F15 R6)."""
    return capability in STEP_UP_CAPABILITIES


# ── principal ─────────────────────────────────────────────────────────────────


def principal_from_claims(claims: dict) -> dict:
    """Shape a JWT payload into a principal: sub, role, tenant, capabilities (F15).

    ``tenant`` defaults to ``"default"`` when the token carries no tenant claim (the single-tenant
    case), so tenant scoping is always well-defined.
    """
    role = claims.get("role", "")
    return {
        "sub": claims.get("sub", role or "anonymous"),
        "role": role,
        "tenant": claims.get("tenant", "default"),
        "capabilities": capabilities_for(role),
    }


# ── enforcement dependency ────────────────────────────────────────────────────


def require_capability(capability: str):
    """FastAPI dependency: 403 unless the caller's role holds ``capability`` (F15 R2)."""

    def _dep(claims: dict = Depends(require_role("viewer"))) -> dict:
        role = claims.get("role", "")
        if not can(role, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=deny_reason(role, capability),
            )
        return claims

    return _dep


# ── tenant scoping (F15 R4, default-deny) ─────────────────────────────────────


def tenant_visible(principal: dict, resource_tenant: str | None) -> bool:
    """Whether ``principal`` may see a resource in ``resource_tenant`` (default-deny).

    Same-tenant is always visible; a resource with no tenant is treated as ``"default"``. A
    cross-tenant **admin** may see other tenants (the multi-tenant-admin case, F15 R4).
    """
    rt = resource_tenant or "default"
    if principal.get("tenant") == rt:
        return True
    return principal.get("role") == "admin"


def assert_tenant_access(principal: dict, resource_tenant: str | None) -> None:
    """Raise 403 when ``principal`` may not access ``resource_tenant`` (F15 R4 / GWT-3)."""
    if not tenant_visible(principal, resource_tenant):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-tenant access denied",
        )


def scope_to_tenant(principal: dict, rows: list[dict], key: str = "tenant") -> list[dict]:
    """Filter ``rows`` to those the principal may see (F15 R4). Rows lacking ``key`` are ``default``."""
    return [r for r in rows if tenant_visible(principal, r.get(key))]

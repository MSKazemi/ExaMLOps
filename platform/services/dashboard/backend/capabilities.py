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
from fastapi import Depends, HTTPException, Request, status

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
# ADR 0119 CLI Console — run `exa` commands. `cli.run` covers the `read` tier (what a viewer can
# already see elsewhere); `cli.write` covers every command that changes state (`admin` and
# `destructive` tiers, and a read that its arguments turn into a write).
CLI_RUN = "cli.run"
CLI_WRITE = "cli.write"

# Actions that additionally require step-up/MFA (F15 R6 / F16, RFC 9470). Enforced by
# `iam_gate.enforce` on every `require_capability` check **when the deployment opts in**: a
# federated user's center has a `step_up` section in the trust file (ADR 0120), or
# `EXAMLOPS_IAM_STEP_UP=enforce` for local password sessions. Opted in, the BFF answers 401
# `insufficient_user_authentication` with `acr_values`/`max_age` and the SPA re-authenticates; not
# opted in, these are permitted on the capability check alone. Keep identical to the frontend's
# `STEP_UP` (guarded).
STEP_UP_CAPABILITIES: frozenset[str] = frozenset({MODEL_PROMOTE, SECRET_REVEAL})

_VIEWER_CAPS: frozenset[str] = frozenset({VIEW, SEARCH, CLI_RUN})
# `operator` (ADR 0120): runs the model lifecycle, holds none of the platform's keys.
_OPERATOR_CAPS: frozenset[str] = _VIEWER_CAPS | frozenset(
    {
        MODEL_PROMOTE,
        APPROVAL_DECIDE,
        RETRAIN_TRIGGER,
        DRIFT_BASELINE,
        TRAFFIC_MANAGE,
    }
)
_ADMIN_CAPS: frozenset[str] = _OPERATOR_CAPS | frozenset(
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
        CLI_WRITE,
    }
)

_CAPS_BY_ROLE: dict[str, frozenset[str]] = {
    "viewer": _VIEWER_CAPS,
    "operator": _OPERATOR_CAPS,
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
    if capability in _OPERATOR_CAPS and role == "viewer":
        return "Requires the operator or admin role."
    if capability in _ADMIN_CAPS and role in {"viewer", "operator"}:
        return "Requires the admin role."
    return f"Your role ('{role}') does not permit '{capability}'."


def requires_step_up(capability: str) -> bool:
    """Whether a capability needs step-up/MFA when the deployment opts in (F15 R6, RFC 9470).

    `iam_gate.enforce` is the request-path caller; see `STEP_UP_CAPABILITIES` for when it bites.
    """
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
        "idp": claims.get("idp"),
        "capabilities": capabilities_for(role),
    }


# ── enforcement dependency ────────────────────────────────────────────────────


def require_capability(capability: str):
    """FastAPI dependency: 403 unless the caller's role holds ``capability`` (F15 R2).

    Then, via `iam_gate.enforce` (ADR 0120): RFC 9470 step-up for designated capabilities, and for
    a federated user the data center's own PDP, which may veto what the role allows.
    """

    def _dep(
        claims: dict = Depends(require_role("viewer")),
        request: Request = None,  # type: ignore[assignment]  # injected; None when called directly
    ) -> dict:
        role = claims.get("role", "")
        if not can(role, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=deny_reason(role, capability),
            )
        from iam_gate import enforce  # noqa: PLC0415 — keeps this module import-light

        enforce(capability, claims, request)
        return claims

    return _dep


# ── tenant scoping (F15 R4, default-deny) ─────────────────────────────────────


def tenant_visible(principal: dict, resource_tenant: str | None) -> bool:
    """Whether ``principal`` may see a resource in ``resource_tenant`` (default-deny).

    Same-tenant is always visible; a resource with no tenant is treated as ``"default"``. A
    cross-tenant **admin** may see other tenants (the multi-tenant-admin case, F15 R4) — but only a
    *platform* admin: an admin whose identity comes from a data center's IdP administers that
    center's tenant and no other (ADR 0120 — a center's admin is not an admin of another center).
    """
    rt = resource_tenant or "default"
    if principal.get("tenant") == rt:
        return True
    return principal.get("role") == "admin" and not principal.get("idp")


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

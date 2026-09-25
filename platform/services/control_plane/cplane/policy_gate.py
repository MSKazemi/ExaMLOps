"""Policy-as-code at the control plane's decision points (ADR 0079 d2, ADR 0029 d3).

The dashboard and the CLI both consult ``examlops.policy`` before a mutation; this is the same
consultation for the routes the control plane owns, and the **one table** that says, for every
mutating route, whether it is gated or why it is not. ``tests/test_policy_route_table.py`` walks
the live app and fails when a mutating route is in neither column — that is what makes the next
mutation consult policy by construction instead of by someone remembering to.

A route is gated where a **CLI-equivalent gate already exists** (same action name, same context
keys, so one ``policy.yaml`` rule governs both doors) and the route is the decision, not a proxy.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

# (METHOD, path with any /v1 prefix removed — a /v1 twin is the same handler, see versioning.py)
#   -> ("gated", action) | ("exempt", reason)
ROUTE_POLICY: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/retrain"): ("gated", "retrain"),
    ("POST", "/webhooks/modelzoo/gitlab"): (
        "exempt",
        "Inbound provider webhook authenticated by the shared secret (no principal to decide "
        "for); it marks models stale and starts CI, and any retrain it causes is governed by "
        "the drift / autopilot gates on that path.",
    ),
    ("POST", "/webhooks/modelzoo/github"): (
        "exempt",
        "Inbound provider webhook authenticated by HMAC signature; same reasoning as the GitLab "
        "webhook.",
    ),
    ("POST", "/api/changes"): (
        "exempt",
        "CI change-notification intake: it only records a *pending* approval. The decision to "
        "approve or reject is the gated one, and a pending row changes no serving state.",
    ),
    ("POST", "/changes"): (
        "exempt",
        "Same handler as /api/changes (CI change-notification intake, records a pending row).",
    ),
    # `exa approvals approve|reject` decide as these actions (examlops.cli._policy_hook), and the
    # dashboard's approve/reject routes use the same names: one rule governs all three doors.
    ("POST", "/approve/{model_id}"): ("gated", "approval_approve"),
    ("POST", "/reject/{model_id}"): ("gated", "approval_reject"),
    ("POST", "/approvals/{model_id}/approve"): ("gated", "approval_approve"),
    ("POST", "/approvals/{model_id}/reject"): ("gated", "approval_reject"),
    ("DELETE", "/approvals/{approval_id}"): (
        "exempt",
        "Retracts a stale pending approval row (kept, marked retracted); resolves nothing and "
        "changes no serving state.",
    ),
    ("DELETE", "/commands/{command_id}"): (
        "exempt",
        "Cancels a queued command the caller already submitted; the submission (retrain) was "
        "the gated decision and a cancel only ever reduces what runs.",
    ),
    # The actions `exa modelzoo sync`, `exa modelzoo config-set` and `exa production reload`
    # decide as — every mutating `exa` command consults policy (examlops.cli._policy_hook).
    ("POST", "/modelzoo/sync"): ("gated", "modelzoo_sync"),
    ("PUT", "/modelzoo/config"): ("gated", "modelzoo_config_set"),
    ("POST", "/admin/reload"): ("gated", "production_reload"),
}


def route_key(method: str, path: str) -> tuple[str, str]:
    """The table key for a live route: ``/v1/x`` and ``/x`` are the same handler."""
    return method.upper(), path[3:] if path.startswith("/v1/") else path


def enforce(action: str, context: dict[str, Any], *, principal: str, tenant: str, request: Request):
    """Raise 403 (deny / engine failure) or 409 (needs approval) — return quietly to proceed.

    ``request`` supplies the acknowledgement header; the caller who sends it is asserting that a
    human approved (``exa retrain`` sends it after its own confirm prompt).
    """
    from examlops.policy import http_gate

    verdict = http_gate.evaluate(
        action,
        {**context, "actor": principal, "tenant": tenant, "via": "control_plane"},
        actor=principal,
        source="control-plane",
        tenant=tenant,
        approved=http_gate.header_asserts_approval(request.headers.get(http_gate.APPROVAL_HEADER)),
    )
    if not verdict.ok:
        raise HTTPException(verdict.status, verdict.detail)


def enforce_retrain(req: Any, context: Any, request: Request) -> None:
    """The ``retrain`` gate — same action and context keys as ``exa retrain`` (one rule, two doors)."""
    enforce(
        "retrain",
        {"model": req.model_name, "dataset": req.dataset_name, "dummy": bool(req.is_dummy)},
        principal=context.principal,
        tenant=context.tenant,
        request=request,
    )


def enforce_approval(
    action: str, model_id: str, reason: str | None, context: Any, request: Request
) -> None:
    """``approval_approve`` / ``approval_reject`` — the keys the CLI and the dashboard supply."""
    enforce(
        action,
        {"model": model_id, "target": model_id, "reason": reason},
        principal=context.principal,
        tenant=context.tenant,
        request=request,
    )


def enforce_admin(action: str, extra: dict[str, Any], context: Any, request: Request) -> None:
    """An admin-scope operational route, gated under its CLI command's action name."""
    enforce(action, extra, principal=context.principal, tenant=context.tenant, request=request)

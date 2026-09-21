"""Policy-as-code on the dashboard's write routes (ADR 0079 decision 2, ADR 0029 decision 3).

The CLI consults ``examlops.policy`` before a mutation (``examlops.cli._policy_gate``); until this
module the dashboard's write routes did not, so an operator's ``policy.yaml`` bound every
``exa`` command and none of the same mutations made from the browser.

Two things live here, and the second is the point:

1. **The gate** - :func:`policy_gate`, ONE app-level dependency (``FastAPI(dependencies=[...])``,
   public API only, identical on every FastAPI release) that matches a request against the gated
   entries of the table by path template and calls
   :func:`examlops.policy.http_gate.evaluate` - the one implementation the control plane shares -
   before the handler runs. It runs before a route's own capability check: with no matching rule
   it is silent and that check answers as before; a matching deny answers first.
2. **The table** — :data:`ROUTE_POLICY`, which classifies **every** mutating route as
   ``gated`` (with its action kind) or ``exempt`` (with the reason). ``tests/test_policy_route_table.py``
   introspects the live app and fails on a mutating route that is in neither column, which is what
   makes a *new* mutation consult policy by construction rather than by memory.

Behaviour-preserving by default: with no ``policy.yaml`` (or no rule for the action) a gated route
answers exactly as before and writes no extra audit row. A rule's decision is 403 naming the rule
(``deny``), 409 until an admin re-sends with ``X-Policy-Approved: true`` (``require_approval`` —
the HTTP form of the CLI's default-no confirmation), or, for a ``mode: monitor`` rule, no effect
beyond an audit row. An engine failure denies and is audited, as it does everywhere else.

Action names reuse the CLI's vocabulary where a CLI equivalent exists (``manual_promote``,
``cluster_approve``, ``cluster_reject``, ``project_delete``, ``project_remove_member``,
``retrain`` at the control plane) so one rule governs both doors; the rest are
``dashboard_<router>_<verb>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from auth import authenticate
from fastapi import FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security.utils import get_authorization_scheme_param

log = logging.getLogger("dashboard.policy_gate")

_MAX_BODY = 64 * 1024
_SENSITIVE = re.compile(r"secret|password|passwd|token|api[_-]?key|credential|private|value", re.I)

# Path parameters that name the model / project the request is about (policy conditions read
# `model`, `project`, `cluster` — the same keys the CLI's context carries).
_MODEL_PARAMS = ("model", "model_id")


@dataclass(frozen=True)
class Gate:
    """A route that consults ``policy.decide`` as ``action`` before it runs."""

    action: str
    extra: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]] | None = None
    """``(path_params, body) -> extra context`` — CLI-vocabulary keys for a shared action."""


@dataclass(frozen=True)
class Exempt:
    """A mutating route that deliberately does not consult policy, and why."""

    reason: str


def _model(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    return {"model": pp.get("model") or pp.get("model_id") or pp.get("name")}


def _cluster(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    return {"cluster": pp.get("name"), "reason": body.get("reason")}


def _project(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    return {"project": pp.get("name")}


def _member(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    return {"project": pp.get("name"), "subject": pp.get("subject"), "target": pp.get("name")}


def _alias_set(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    alias = body.get("alias")
    return {
        "model": pp.get("name"),
        "version": str(pp.get("version")),
        "to_alias": alias,
        "from_alias": body.get("from_alias"),
    }


def _alias_delete(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    return {"model": pp.get("name"), "version": str(pp.get("version")), "alias": pp.get("alias")}


def _model_id(pp: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    return {"model": pp.get("model_id"), "reason": body.get("reason")}


_G = Gate
_X = Exempt

_PROXY = (
    "Ships as a reverse proxy to a backing service, which enforces its own credential; the "
    "mutations that matter are exposed as first-class (gated) routes, and this is the same "
    "trust boundary as calling the service directly."
)
_CLI = (
    "The CLI process this route runs enforces policy itself (ADR 0119: every `exa` mutation calls "
    "policy.decide before acting) — gating here as well would evaluate one rule twice."
)
_SESSION = "Authentication / session lifecycle: no platform resource is changed."

# (METHOD, full path) -> Gate | Exempt. Every mutating route of the dashboard app must appear here.
ROUTE_POLICY: dict[tuple[str, str], Gate | Exempt] = {
    # ── facility / fleet ───────────────────────────────────────────────────────────────────
    ("POST", "/api/v1/facility/fleet/{name}/approve"): _G("cluster_approve", _cluster),
    ("POST", "/api/v1/facility/fleet/{name}/reject"): _G("cluster_reject", _cluster),
    # ── self-observability / alerts / flags / collaboration ─────────────────────────────────
    ("POST", "/api/v1/selfobs/action"): _X(
        "UI interaction beacon feeding the self-observability metrics; changes no platform state."
    ),
    ("POST", "/api/v1/alerts/{alert_id}/ack"): _X(
        "Acknowledging an alert annotates its display state only; the alert rule and the "
        "condition behind it are untouched."
    ),
    ("POST", "/api/v1/flags/{name}"): _G("dashboard_flags_set"),
    ("POST", "/api/v1/copilot/ask"): _X(
        "Read-only-by-POST: the copilot is propose-only (F11) and mutates nothing itself; every "
        "action it proposes is a separate, gated request."
    ),
    ("POST", "/api/v1/collab/{entity_type}/{entity_id}/comments"): _X(
        "Collaboration content (F22): a comment is user text attached to an entity, not platform "
        "state."
    ),
    ("POST", "/api/v1/collab/snapshot"): _X(
        "Collaboration content (F22): a shareable read-only snapshot of a view."
    ),
    # ── auth / SSO / SCIM ───────────────────────────────────────────────────────────────────
    ("POST", "/api/auth/login"): _X(_SESSION),
    ("POST", "/api/auth/logout"): _X(_SESSION),
    ("POST", "/api/auth/sso/logout"): _X(_SESSION),
    ("POST", "/api/scim/v2/Users"): _X(
        "SCIM provisioning is authenticated by each center's own bearer, has no dashboard "
        "principal to decide for, and is governed by the IAM trust file (ADR 0120/0132)."
    ),
    ("PUT", "/api/scim/v2/Users/{account_id}"): _X("SCIM provisioning (ADR 0132); see POST."),
    ("PATCH", "/api/scim/v2/Users/{account_id}"): _X("SCIM provisioning (ADR 0132); see POST."),
    ("DELETE", "/api/scim/v2/Users/{account_id}"): _X("SCIM deprovisioning (ADR 0132); see POST."),
    # ── config / proxy ──────────────────────────────────────────────────────────────────────
    ("PUT", "/api/config"): _G("dashboard_config_write"),
    ("POST", "/api/config/export-env"): _X(
        "Read-only-by-POST: renders the stored config as a downloadable .env file; nothing is "
        "written."
    ),
    ("POST", "/api/config/import-env"): _G("dashboard_config_import"),
    ("DELETE", "/api/proxy/{service}/{path:path}"): _X(_PROXY),
    ("PATCH", "/api/proxy/{service}/{path:path}"): _X(_PROXY),
    ("POST", "/api/proxy/{service}/{path:path}"): _X(_PROXY),
    ("PUT", "/api/proxy/{service}/{path:path}"): _X(_PROXY),
    # ── models ──────────────────────────────────────────────────────────────────────────────
    ("PUT", "/api/models/{name}/versions/{version}/alias"): _G("manual_promote", _alias_set),
    ("DELETE", "/api/models/{name}/versions/{version}/alias/{alias}"): _G(
        "dashboard_models_alias_delete", _alias_delete
    ),
    ("POST", "/api/models/{name}/predict"): _X(
        "Read-only-by-POST: runs one inference; nothing is registered, promoted or stored."
    ),
    ("PUT", "/api/models/{name}/description"): _X(
        "Model README text (documentation content); it does not change lifecycle or serving."
    ),
    ("DELETE", "/api/models/{name}/description"): _X("Model README text; see PUT."),
    ("POST", "/api/models/{name}/images"): _X("Model README gallery image; documentation content."),
    ("DELETE", "/api/models/{name}/images/{image_id}"): _X("Model README gallery image; see POST."),
    ("POST", "/api/modelzoo/trigger-pipeline"): _G("dashboard_modelzoo_trigger_pipeline"),
    # ── containers ──────────────────────────────────────────────────────────────────────────
    ("POST", "/api/containers/{service}/start"): _G("dashboard_containers_start"),
    ("POST", "/api/containers/{service}/stop"): _G("dashboard_containers_stop"),
    ("POST", "/api/containers/{service}/restart"): _G("dashboard_containers_restart"),
    # ── approvals / pipelines / scaffold ────────────────────────────────────────────────────
    ("POST", "/api/approvals/approve/{model_id}"): _G("approval_approve", _model_id),
    ("POST", "/api/approvals/reject/{model_id}"): _G("approval_reject", _model_id),
    ("POST", "/api/pipelines/trigger"): _X(
        "Proxy of `POST /v1/retrain`, where the control plane gates `retrain` (same action and "
        "context as `exa retrain`); the acknowledgement header is forwarded. Gating here too "
        "would evaluate the rule twice."
    ),
    ("POST", "/api/scaffold/preview"): _X("Read-only-by-POST: renders a scaffold, writes nothing."),
    ("POST", "/api/scaffold/create"): _G("dashboard_scaffold_create"),
    # ── drift ───────────────────────────────────────────────────────────────────────────────
    ("POST", "/api/drift/baseline/{model}"): _G("dashboard_drift_baseline", _model),
    ("POST", "/api/drift/reset/{model}"): _G("dashboard_drift_reset", _model),
    ("POST", "/api/drift/auto-retrain/{model}"): _G("dashboard_drift_auto_retrain", _model),
    ("POST", "/api/drift/input-baseline/{model}"): _G("dashboard_drift_input_baseline", _model),
    ("POST", "/api/drift/input-reset/{model}"): _G("dashboard_drift_input_reset", _model),
    # ── compliance / gateway / prompts / autopilot / slo ────────────────────────────────────
    ("POST", "/api/compliance/classify/{model}"): _G("dashboard_compliance_classify", _model),
    ("POST", "/api/compliance/conformity/{model}"): _G("dashboard_compliance_conformity", _model),
    ("POST", "/api/compliance/technical-file/{model}"): _G(
        "dashboard_compliance_technical_file", _model
    ),
    ("POST", "/api/gateway/keys"): _G("dashboard_gateway_issue_key"),
    ("POST", "/api/gateway/keys/{key_hash}/revoke"): _G("dashboard_gateway_revoke_key"),
    ("POST", "/api/prompts/{name}/versions"): _G("dashboard_prompts_create_version"),
    ("POST", "/api/prompts/{name}/label"): _G("dashboard_prompts_set_label"),
    ("POST", "/api/autopilot/enable"): _G("dashboard_autopilot_enable"),
    ("POST", "/api/autopilot/disable"): _G("dashboard_autopilot_disable"),
    ("POST", "/api/slo"): _G("dashboard_slo_set"),
    # ── scaling / admission / events / traffic ──────────────────────────────────────────────
    ("POST", "/api/v1/scaling/autoscale"): _G("dashboard_scaling_autoscale"),
    ("POST", "/api/v1/scaling/routing"): _G("dashboard_scaling_routing"),
    ("POST", "/api/v1/admission"): _G("dashboard_admission_submit"),
    ("POST", "/api/v1/events"): _G("dashboard_events_publish"),
    ("POST", "/api/v1/traffic/ab/start"): _G("dashboard_traffic_ab_start"),
    ("POST", "/api/v1/traffic/ab/stop"): _G("dashboard_traffic_ab_stop"),
    ("POST", "/api/v1/traffic/shadow"): _G("dashboard_traffic_shadow"),
    # ── secrets / feature store / fairness / platform data ──────────────────────────────────
    ("POST", "/api/secrets"): _G("dashboard_secrets_set"),
    ("POST", "/api/feature-store/views"): _G("dashboard_feature_store_apply_view"),
    ("POST", "/api/fairness"): _G("dashboard_fairness_set"),
    ("PUT", "/api/platform-data/traffic-rules/{model}"): _G(
        "dashboard_platform_data_traffic_rules_set", _model
    ),
    # ── platform ops: already routed through PlatformAdmin ──────────────────────────────────
    ("POST", "/api/v1/platform-ops/cost"): _X(
        "Already gated: the handler calls `examlops.platform_admin.PlatformAdmin`, which runs "
        "RBAC then `policy.decide_safe('platform_admin:<action>')` and audits (BL-080)."
    ),
    ("POST", "/api/v1/platform-ops/provider"): _X("Already gated via PlatformAdmin; see cost."),
    ("POST", "/api/v1/platform-ops/knob"): _X("Already gated via PlatformAdmin; see cost."),
    # ── challenger ──────────────────────────────────────────────────────────────────────────
    ("POST", "/api/challenger/{model}/promote"): _G("dashboard_challenger_promote", _model),
    ("POST", "/api/challenger/{model}/disable"): _G("dashboard_challenger_disable", _model),
    # ── projects ────────────────────────────────────────────────────────────────────────────
    ("PUT", "/api/v1/projects/{name}"): _G("dashboard_projects_update", _project),
    ("POST", "/api/v1/projects"): _G("dashboard_projects_create"),
    ("POST", "/api/v1/projects/{name}/resources"): _G("dashboard_projects_assign", _project),
    ("POST", "/api/v1/projects/{name}/members"): _G("dashboard_projects_add_member", _project),
    ("DELETE", "/api/v1/projects/{name}/members/{subject}"): _G("project_remove_member", _member),
    ("POST", "/api/v1/projects/{name}/storage"): _G("dashboard_projects_storage", _project),
    ("DELETE", "/api/v1/projects/{name}"): _G("project_delete", _project),
    ("POST", "/api/v1/projects/onboard/{model}"): _G("dashboard_projects_onboard", _model),
    ("POST", "/api/v1/projects/onboard-all"): _G("dashboard_projects_onboard_all"),
    # ── providers ───────────────────────────────────────────────────────────────────────────
    ("POST", "/api/v1/providers/validate"): _X(
        "Read-only-by-POST: dry-run validation of a provider expression; nothing is saved."
    ),
    ("POST", "/api/v1/providers"): _G("dashboard_providers_save"),
    ("POST", "/api/v1/providers/{project}/{domain}/{name}/activate"): _G(
        "dashboard_providers_activate"
    ),
    ("DELETE", "/api/v1/providers/{project}/{domain}/{name}"): _G("dashboard_providers_delete"),
    # ── connections / workbenches ───────────────────────────────────────────────────────────
    ("POST", "/api/v1/connections"): _G("dashboard_connections_create"),
    ("POST", "/api/v1/connections/{name}/test"): _X(
        "Read-only-by-POST: a reachability probe of an already-registered connection; it stores "
        "nothing and its egress is bounded by the dataplane allow-list."
    ),
    ("DELETE", "/api/v1/connections/{name}"): _G("dashboard_connections_delete"),
    ("POST", "/api/v1/workbenches"): _G("dashboard_workbenches_create"),
    ("POST", "/api/v1/workbenches/{project}/{name}/status"): _G("dashboard_workbenches_status"),
    ("DELETE", "/api/v1/workbenches/{project}/{name}"): _G("dashboard_workbenches_delete"),
    # ── CLI console ─────────────────────────────────────────────────────────────────────────
    ("POST", "/api/v1/cli/runs"): _X(_CLI),
    ("POST", "/api/v1/cli/runs/{run_id}/cancel"): _X(
        "Cancels a run the caller started; the run itself was the (CLI-gated) decision."
    ),
    ("POST", "/api/v1/cli/workspace"): _X(
        "Uploads a file into the console's isolated, size-capped workspace (ADR 0119) — a scratch "
        "area for command arguments, not platform state."
    ),
    ("DELETE", "/api/v1/cli/workspace/file"): _X("Removes a file from the isolated workspace."),
}

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def normalize(path: str) -> str:
    """``/x/{p:path}`` -> ``/x/{p}``: OpenAPI drops the converter, Starlette's routes keep it."""
    return re.sub(r"\{(\w+):\w+\}", r"{\1}", path)


def openapi_mutating(app: FastAPI) -> set[tuple[str, str]]:
    """``(METHOD, path template)`` for every mutating operation in ``app.openapi()``.

    This is the **source of truth** for the guard test because ``openapi()`` is public API and
    means the same on every FastAPI release; ``app.routes`` does not (FastAPI 0.141 stopped
    flattening ``include_router``). Its limit: an operation declared with
    ``include_in_schema=False`` is absent — :func:`walk_mutating` adds what a recursive walk of
    ``app.routes`` can still see, on a best-effort basis.
    """
    return {
        (method.upper(), normalize(path))
        for path, ops in app.openapi().get("paths", {}).items()
        for method in ops
        if method.upper() in MUTATING
    }


def walk_mutating(routes: Any, prefix: str = "") -> set[tuple[str, str]]:
    """Best-effort ``(METHOD, path)`` walk of ``app.routes`` (duck-typed, nested routers too)."""
    found: set[tuple[str, str]] = set()
    for route in routes:
        path = prefix + str(getattr(route, "path", "") or "")
        methods = getattr(route, "methods", None)
        if methods:
            found |= {(m, normalize(path)) for m in methods if m in MUTATING}
        nested = getattr(route, "routes", None)
        if nested:
            found |= walk_mutating(nested, path)
    return found


def _compile(template: str) -> re.Pattern[str]:
    """``/a/{x}/b/{p:path}`` -> a regex with one named group per path parameter."""
    out, pos = [], 0
    for m in re.finditer(r"\{(\w+)(?::(\w+))?\}", template):
        out.append(re.escape(template[pos : m.start()]))
        out.append(f"(?P<{m.group(1)}>{'.+' if m.group(2) == 'path' else '[^/]+'})")
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.compile("^" + "".join(out) + "/?$")


def _specificity(template: str) -> tuple[int, int]:
    """More literal characters first, fewer parameters first: the most specific template wins."""
    literal = re.sub(r"\{[^}]*\}", "", template)
    return (-len(literal), template.count("{"))


_COMPILED: list[tuple[str, re.Pattern[str], str, Gate | Exempt]] = sorted(
    ((method, _compile(path), path, entry) for (method, path), entry in ROUTE_POLICY.items()),
    key=lambda t: _specificity(t[2]),
)


def resolve(method: str, path: str) -> tuple[str, Gate | Exempt, dict[str, str]] | None:
    """The table entry that governs a concrete request, with its path parameters, or ``None``."""
    for m, pattern, template, entry in _COMPILED:
        if m == method:
            hit = pattern.match(path)
            if hit:
                return template, entry, hit.groupdict()
    return None


def _claims(request: Request) -> dict:
    """The caller's claims via the dashboard's own ``authenticate`` — same 401/403 semantics.

    Read from the raw header rather than a ``Security(...)`` parameter: an app-level dependency
    that declared one would add a bearer requirement to *every* operation in the OpenAPI schema.
    """
    scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
    creds = (
        HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
        if scheme.lower() == "bearer" and token
        else None
    )
    return authenticate(request, creds)


async def policy_gate(request: Request) -> None:
    """App-level dependency: consult policy for a request that hits a *gated* route.

    Registered once on the ``FastAPI(dependencies=[...])`` app, so it uses only public
    FastAPI/Starlette API and behaves the same on every version. It matches the request against
    :data:`ROUTE_POLICY` by path template and does nothing for anything else (safe methods,
    exempt routes, unknown paths, unauthenticated endpoints such as login).

    **Precedence.** App-level dependencies run before a route's own ones, so for a gated route
    this runs *before* its capability check: with no matching rule it allows silently and the
    route's own 403 then fires exactly as before; a matching ``deny`` rule answers first (403
    naming the rule). A ``require_approval`` rule answers an admin with 409 (re-send with the
    acknowledgement) and anyone else with 403 — a non-admin never gets an invitation to approve.
    """
    if request.method not in MUTATING:
        return
    hit = resolve(request.method, request.url.path)
    if hit is None:
        return
    _template, gate, pp = hit
    if not isinstance(gate, Gate):
        return
    from capabilities import principal_from_claims

    from examlops.policy import http_gate

    principal = principal_from_claims(_claims(request))  # 401 for an unauthenticated caller
    body: dict[str, Any] = {}
    if request.method != "DELETE" and "json" in request.headers.get("content-type", ""):
        raw = await request.body()
        if len(raw) <= _MAX_BODY:
            body = _body_scalars(raw)
    first = next(iter(pp.values()), "")
    context: dict[str, Any] = {
        **{f"body_{k}": v for k, v in body.items()},
        **pp,
        "target": str(first),
        "role": principal["role"],
        "method": request.method,
        "path": request.url.path,
    }
    if gate.extra is not None:
        context.update({k: v for k, v in gate.extra(pp, body).items() if v is not None})
    is_admin = principal["role"] == "admin"
    verdict = await asyncio.to_thread(
        http_gate.evaluate,
        gate.action,
        {**context, "actor": principal["sub"], "tenant": principal["tenant"], "via": "dashboard"},
        actor=principal["sub"],
        source="dashboard-policy",
        tenant=principal["tenant"],
        approved=http_gate.header_asserts_approval(request.headers.get(http_gate.APPROVAL_HEADER)),
        approver_ok=is_admin,
    )
    if verdict.ok:
        return
    if verdict.status == 409 and not is_admin:
        # Only an admin may approve. Falling through would let an operator with the capability
        # skip the approval the rule demands, so this is a refusal, not a 409 invitation.
        raise HTTPException(
            403,
            f"Policy rule '{verdict.rule}' requires approval by an admin for {gate.action}.",
            headers={"X-Policy-Rule": str(verdict.rule)},
        )
    headers = {"X-Policy-Rule": verdict.rule} if verdict.rule else None
    raise HTTPException(verdict.status, verdict.detail, headers=headers)


def _body_scalars(raw: bytes) -> dict[str, Any]:
    """Top-level scalar fields of a JSON body, minus anything that looks like a credential."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, str | int | float | bool) and not _SENSITIVE.search(str(k))
    }

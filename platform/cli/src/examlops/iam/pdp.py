"""Authorization: the platform's own policy, combined with the data center's PDP (ADR 0120).

Every enforcement point (PEP) — a dashboard route, a control-plane route, a CLI mutation — asks
one question, ``authorize(principal, action, resource)``, and gets one :class:`Decision`.

Three layers, evaluated in this order:

1. **Tenant isolation (invariant).** A federated principal acts only inside the tenant its issuer is
   bound to. No role and no external policy can lift this: a center's admin is not an admin of
   another center.
2. **Local policy.** The platform role ladder (viewer < operator < admin) against a per-action
   minimum, raised by project-scoped roles from the IdP and by relationship grants
   (``examlops.authz`` ReBAC, when multi-tenancy is on). A service may pass its own local verdict
   (the dashboard's capability map) instead.
3. **The center's PDP**, when the provider's ``authorization.mode`` is ``external`` or ``both``:

   * **OpenID AuthZEN** Access Evaluation API — ``POST <pdp>/access/v1/evaluation`` with
     ``{subject, action, resource, context}`` → ``{"decision": bool, "context": {...}}``.
   * **Open Policy Agent** Data API — ``POST <pdp>/v1/data/<path>`` with ``{"input": …}`` →
     ``{"result": bool | {"allow": bool, "reason": …}}``; an undefined result is a deny.

   ``both`` is **deny-overrides**: the platform and the center must both allow. A PDP that errors or
   times out denies (``on_error: deny``, the default); ``on_error: local`` is an explicit opt-in to
   fall back to the local verdict. PDP *permits* are cached for ``cache_ttl_s`` (default
   30 s); denies and errors never are.

Every deny is written to the audit log as ``authz_denied`` with the layer that denied and why.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from examlops.iam.config import ROLE_RANK, IamConfig, PdpConfig, load_config, resolve_secret_ref
from examlops.iam.tokens import Principal

logger = logging.getLogger(__name__)

# Minimum platform role per action. Actions are `<domain>.<verb>`; the dashboard's capability
# names are used verbatim so one catalogue serves every PEP. Unknown actions need `admin`
# (default-deny for anything nobody classified).
ACTION_MIN_ROLE: dict[str, str] = {
    "view": "viewer",
    "search": "viewer",
    "api.read": "viewer",
    "cli.run": "viewer",
    "api.write": "operator",
    "retrain.trigger": "operator",
    "model.promote": "operator",
    "approval.decide": "operator",
    "drift.baseline": "operator",
    "traffic.manage": "operator",
    "admission.manage": "operator",
    "cli.write": "admin",
}
_READ_SUFFIXES = (".read", ".view", ".list", ".get")


def min_role_for(action: str) -> str:
    if action in ACTION_MIN_ROLE:
        return ACTION_MIN_ROLE[action]
    if action.endswith(_READ_SUFFIXES):
        return "viewer"
    return "admin"


_RELATION_FOR_ROLE = {"viewer": "viewer", "operator": "editor", "admin": "owner"}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    layer: str = "local"  # tenant | local | pdp | combined
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "layer": self.layer,
            "details": dict(self.details),
        }


class PdpError(RuntimeError):
    """The external PDP could not produce a decision."""


# ── local policy ──────────────────────────────────────────────────────────────


def effective_role(principal: Principal, resource: dict[str, Any] | None) -> str | None:
    """Global role raised by a project-scoped role for the resource's project."""
    role = principal.role
    project = (resource or {}).get("project")
    if project and project in principal.projects:
        proj_role = principal.projects[project]
        if ROLE_RANK.get(proj_role, 0) > ROLE_RANK.get(role or "", 0):
            role = proj_role
    return role


def local_decision(principal: Principal, action: str, resource: dict[str, Any] | None) -> Decision:
    need = min_role_for(action)
    role = effective_role(principal, resource)
    if ROLE_RANK.get(role or "", 0) >= ROLE_RANK[need]:
        return Decision(True, f"role {role} ≥ {need}", "local")
    obj = (resource or {}).get("object")
    if obj:
        try:
            from examlops import authz

            if authz.multitenancy_enabled() and authz.check(
                principal.id, _RELATION_FOR_ROLE[need], obj, actor=principal.actor
            ):
                return Decision(True, f"relation {_RELATION_FOR_ROLE[need]} on {obj}", "local")
        except Exception as exc:  # noqa: BLE001 — a relationship-store failure is a deny
            logger.error("ReBAC check failed closed for %s on %s: %s", principal.id, obj, exc)
    return Decision(False, f"role {role or 'none'} < {need} required for {action}", "local")


# ── external PDP clients ──────────────────────────────────────────────────────


def _pdp_headers(pdp: PdpConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = resolve_secret_ref(pdp.token_ref) if pdp.token_ref else None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def authzen_request(
    principal: Principal, action: str, resource: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """The AuthZEN Access Evaluation request body for one decision."""
    rtype = str(resource.get("type", "platform"))
    rid = str(resource.get("id", resource.get("object", "*")))
    props = {k: v for k, v in resource.items() if k not in {"type", "id"}}
    return {
        "subject": {
            "type": "user",
            "id": principal.id,
            "properties": {
                "issuer": principal.issuer,
                "username": principal.username,
                "email": principal.email,
                "tenant": principal.tenant,
                "role": principal.role,
                "groups": list(principal.groups),
                "scopes": list(principal.scopes),
                "acr": principal.acr,
            },
        },
        "action": {"name": action},
        "resource": {"type": rtype, "id": rid, "properties": props},
        "context": context,
    }


def _post(url: str, body: dict[str, Any], pdp: PdpConfig) -> dict[str, Any]:
    import httpx

    try:
        resp = httpx.post(url, json=body, headers=_pdp_headers(pdp), timeout=pdp.timeout_s)
    except httpx.HTTPError as exc:
        raise PdpError(f"PDP {url} unreachable: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        raise PdpError(f"PDP {url} answered HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise PdpError(f"PDP {url} returned non-JSON") from exc
    if not isinstance(data, dict):
        raise PdpError(f"PDP {url} returned a non-object")
    return data


def ask_pdp(
    pdp: PdpConfig,
    principal: Principal,
    action: str,
    resource: dict[str, Any],
    context: dict[str, Any],
) -> Decision:
    """One uncached decision from the center's PDP. Raises :class:`PdpError`."""
    body = authzen_request(principal, action, resource, context)
    if pdp.type == "authzen":
        data = _post(f"{pdp.url}/access/v1/evaluation", body, pdp)
        decision = data.get("decision")
        if not isinstance(decision, bool):
            raise PdpError("AuthZEN response has no boolean 'decision'")
        raw_ctx = data.get("context")
        ctx: dict[str, Any] = raw_ctx if isinstance(raw_ctx, dict) else {}
        reason = ctx.get("reason_user") or ctx.get("reason_admin") or ctx.get("reason") or ""
        if isinstance(reason, dict):
            reason = next(iter(reason.values()), "") if reason else ""
        return Decision(
            decision, str(reason) or f"center PDP {'permit' if decision else 'deny'}", "pdp"
        )
    data = _post(f"{pdp.url}/v1/data/{pdp.opa_path}", {"input": body}, pdp)
    result = data.get("result")
    if isinstance(result, bool):
        return Decision(result, f"OPA {pdp.opa_path} = {str(result).lower()}", "pdp")
    if isinstance(result, dict) and isinstance(result.get("allow"), bool):
        return Decision(result["allow"], str(result.get("reason", "")) or "OPA policy", "pdp")
    # Undefined document (no rule fired) or an unexpected shape: OPA's own convention is deny.
    return Decision(False, f"OPA {pdp.opa_path} undefined — deny", "pdp")


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Decision]] = {}


def _cache_key(provider: str, principal: Principal, action: str, resource: dict[str, Any]) -> str:
    # Everything the PDP is shown about the subject is part of the key: a permit granted to the
    # user's MFA session (acr) or with a group they have since lost must not be served to another
    # session of the same user.
    return json.dumps(
        [
            provider,
            principal.id,
            principal.role,
            principal.tenant,
            principal.acr,
            sorted(principal.groups),
            sorted(principal.scopes),
            action,
            resource,
        ],
        sort_keys=True,
        default=str,
    )


def cached_pdp_decision(
    pdp: PdpConfig,
    provider: str,
    principal: Principal,
    action: str,
    resource: dict[str, Any],
    context: dict[str, Any],
) -> Decision:
    key = _cache_key(provider, principal, action, resource)
    now = time.monotonic()
    if pdp.cache_ttl_s > 0:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
    decision = ask_pdp(pdp, principal, action, resource, context)
    # Only permits are cached: a cached deny would delay a grant the center just made, and an
    # error is never cached at all (it raised above). The TTL bounds how long a revocation at the
    # center takes to bite — the same staleness trade-off RFC 7662 §4 describes for introspection.
    if pdp.cache_ttl_s > 0 and decision.allowed:
        with _cache_lock:
            if len(_cache) > 50_000:
                _cache.clear()
            _cache[key] = (now + pdp.cache_ttl_s, decision)
    return decision


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ── the one entry point ───────────────────────────────────────────────────────


def _audit_deny(principal: Principal, action: str, resource: dict[str, Any], d: Decision) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "iam",
            principal.actor,
            "authz_denied",
            str(resource.get("id") or resource.get("object") or resource.get("type") or "platform"),
            {
                "principal": principal.id,
                "tenant": principal.tenant,
                "action": action,
                "layer": d.layer,
                "reason": d.reason,
            },
        )
    except Exception as exc:  # noqa: BLE001 — never block a deny on audit, never lose it silently
        logger.warning("authz_denied audit write failed for %s/%s: %s", principal.id, action, exc)


def authorize(
    principal: Principal,
    action: str,
    resource: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    *,
    config: IamConfig | None = None,
    local_allowed: bool | None = None,
    audit: bool = True,
) -> Decision:
    """Decide whether ``principal`` may perform ``action`` on ``resource``.

    ``resource`` is a flat mapping; recognised keys are ``type``, ``id``, ``tenant``, ``project``
    and ``object`` (a ReBAC object such as ``project:acme/model:JPCP``); anything else is passed to
    the center's PDP as a resource property. ``local_allowed`` substitutes the calling service's
    own local verdict for the built-in role catalogue.
    """
    resource = dict(resource or {})
    ctx = {"time": datetime.now(UTC).isoformat(timespec="seconds"), **(context or {})}

    def _done(d: Decision) -> Decision:
        if not d.allowed and audit:
            _audit_deny(principal, action, resource, d)
        return d

    rt = resource.get("tenant")
    if rt is not None and str(rt) != principal.tenant:
        return _done(
            Decision(
                False,
                f"cross-tenant access denied: principal tenant {principal.tenant!r}, "
                f"resource tenant {rt!r}",
                "tenant",
            )
        )
    resource.setdefault("tenant", principal.tenant)

    if local_allowed is None:
        local = local_decision(principal, action, resource)
    else:
        local = Decision(local_allowed, "service policy " + ("permit" if local_allowed else "deny"))

    if config is None:
        try:
            config = load_config()
        except Exception:  # noqa: BLE001 — a broken trust file must not open anything up
            return _done(Decision(False, "identity federation misconfigured", "combined"))
    provider = config.by_name(principal.provider)
    mode = provider.authorization_mode if provider else "local"
    pdp = provider.pdp if provider else None
    if mode == "local" or pdp is None:
        return _done(local)
    if mode == "both" and not local.allowed:
        return _done(local)  # deny-overrides: no need to ask the center

    try:
        remote = cached_pdp_decision(pdp, principal.provider, principal, action, resource, ctx)
    except PdpError as exc:
        if pdp.on_error == "local":
            logger.warning("PDP error for %s, falling back to local policy: %s", principal.id, exc)
            return _done(
                Decision(local.allowed, f"{local.reason} (PDP unavailable: {exc})", "local")
            )
        return _done(Decision(False, f"center PDP unavailable ({exc}) — fail closed", "pdp"))

    if mode == "external":
        return _done(remote)
    allowed = local.allowed and remote.allowed
    reason = local.reason if not local.allowed else remote.reason
    return _done(
        Decision(
            allowed,
            reason,
            "combined" if allowed else remote.layer,
            {"local": local.reason, "pdp": remote.reason},
        )
    )

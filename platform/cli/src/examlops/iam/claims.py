"""Claim mapping: from what a center's IdP asserts to what ExaMLOps understands (ADR 0120).

Centers speak different dialects for the same fact ("this person may administer ML models"):

* Keycloak — ``groups: ["/examlops/admins"]``, ``realm_access.roles``, ``resource_access.<client>.roles``
* LDAP-backed IdPs — group DNs, ``cn=examlops-admins,ou=groups,dc=fz-juelich,dc=de``
* Research AAIs (Helmholtz AAI, EGI Check-in, MyAccessID) — AARC-G002 entitlements,
  ``urn:geant:helmholtz.de:group:examlops:admins:role=owner#login.helmholtz.de``
* OAuth scopes — ``scope: "openid examlops.operate"``

This module collects the candidate values from the configured claim paths, adds the normalised
forms (DN → CN, AARC-G002 URN → group path, Keycloak path → last segment), and applies the
provider's ordered role rules. The strongest matching role wins; no match and no ``default_role``
means *authenticated but not authorized* — the caller answers 403, never a silent viewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from examlops.iam.config import ROLE_RANK, ProviderConfig

# AARC-G002: urn:<NID>:<delegated-namespace>[:<subnamespace>...]:group:<GROUP>[:<SUBGROUP>...]
#            [:role=<ROLE>]#<GROUP-AUTHORITY>
_AARC_RE = re.compile(
    r"^urn:(?P<nid>[^:]+):(?P<ns>.+?):group:(?P<group>[^#]+?)(?::role=(?P<role>[^#:]+))?"
    r"#(?P<authority>.+)$"
)


@dataclass(frozen=True)
class Entitlement:
    """A parsed AARC-G002 group entitlement."""

    namespace: str
    group: str  # colon-separated path, e.g. "examlops:admins"
    role: str | None
    authority: str


def parse_entitlement(value: str) -> Entitlement | None:
    """Parse an AARC-G002 entitlement URN, or ``None`` when ``value`` is not one."""
    m = _AARC_RE.match(value)
    if not m:
        return None
    return Entitlement(
        namespace=f"{m.group('nid')}:{m.group('ns')}",
        group=m.group("group"),
        role=m.group("role"),
        authority=m.group("authority"),
    )


def get_path(claims: dict[str, Any], path: str) -> Any:
    """Resolve a dot-path (``resource_access.examlops.roles``) in a claims object.

    A literal key containing dots (some IdPs emit ``"https://example.org/groups"``) is tried
    first, so URL-named claims work without escaping.
    """
    if path in claims:
        return claims[path]
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _as_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Space- or comma-delimited single strings occur in the wild (scope-style group claims).
        return [v for v in re.split(r"[\s,]+", value) if v]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if isinstance(v, (str, int))]
    return []


def _normalised_forms(value: str) -> list[str]:
    """Every form a rule may reasonably be written against, raw value first."""
    forms = [value]
    ent = parse_entitlement(value)
    if ent is not None:
        forms.append(ent.group)
        if ent.role:
            forms.append(f"{ent.group}:role={ent.role}")
    if "=" in value and "," in value:  # LDAP DN → its leading RDN value
        first = value.split(",", 1)[0]
        if "=" in first:
            forms.append(first.split("=", 1)[1])
    if value.startswith("/"):  # Keycloak group path → full path without slash + leaf
        forms.append(value.strip("/"))
        forms.append(value.rstrip("/").rsplit("/", 1)[-1])
    seen: set[str] = set()
    return [f for f in forms if not (f in seen or seen.add(f))]


def collect_groups(provider: ProviderConfig, claims: dict[str, Any]) -> dict[str, list[str]]:
    """``{claim_path: [raw values]}`` for every configured group claim present in ``claims``."""
    out: dict[str, list[str]] = {}
    for path in provider.group_claims:
        values = _as_strings(get_path(claims, path))
        if values:
            out[path] = values
    return out


def scopes_of(claims: dict[str, Any]) -> tuple[str, ...]:
    scope = claims.get("scope", claims.get("scp", ""))
    return tuple(_as_strings(scope))


@dataclass(frozen=True)
class RoleMapping:
    role: str | None
    groups: tuple[str, ...]
    projects: dict[str, str]  # project → role granted by a project-scoped rule
    matched: tuple[str, ...]  # human-readable explanation of every rule that fired


def map_roles(provider: ProviderConfig, claims: dict[str, Any]) -> RoleMapping:
    """Apply the provider's role rules and scope mapping to verified ``claims``."""
    by_claim = collect_groups(provider, claims)
    groups = tuple(v for values in by_claim.values() for v in values)
    best: str | None = None
    projects: dict[str, str] = {}
    matched: list[str] = []

    def _consider(role: str, why: str, rule_projects: tuple[str, ...] = ()) -> None:
        nonlocal best
        matched.append(why)
        if rule_projects:
            for proj in rule_projects:
                if ROLE_RANK[role] > ROLE_RANK.get(projects.get(proj, ""), 0):
                    projects[proj] = role
            return
        if best is None or ROLE_RANK[role] > ROLE_RANK[best]:
            best = role

    for rule in provider.role_rules:
        paths = [rule.claim] if rule.claim else list(by_claim)
        for path in paths:
            raw_values = (
                by_claim.get(path) if rule.claim is None else _as_strings(get_path(claims, path))
            )
            for raw in raw_values or []:
                if any(rule.matches(form) for form in _normalised_forms(raw)):
                    _consider(rule.role, f"{path}={raw} → {rule.role}", rule.projects)
                    break

    for scope in scopes_of(claims):
        role = provider.scope_roles.get(scope)
        if role:
            _consider(role, f"scope {scope} → {role}")

    if best is None and provider.default_role:
        best = provider.default_role
        matched.append(f"default_role → {best}")
    return RoleMapping(best, groups, projects, tuple(matched))


class TenantError(ValueError):
    """The token asserts a tenant its issuer is not trusted for."""


def resolve_tenant(provider: ProviderConfig, claims: dict[str, Any]) -> str:
    """The tenant for a verified token, enforcing the issuer→tenant binding.

    A fixed ``tenant`` wins. A claim-derived tenant must be in ``tenants_allowed`` — otherwise a
    compromised or misconfigured center IdP could assert *another* center's tenant and read its
    models (the cross-tenant confusion the binding exists to prevent).
    """
    if provider.tenant:
        return provider.tenant
    raw = get_path(claims, provider.tenant_claim or "tenant")
    tenant = str(raw).strip() if isinstance(raw, (str, int)) and str(raw).strip() else "default"
    allowed = provider.tenants_allowed
    if "*" in allowed or tenant in allowed:
        return tenant
    raise TenantError(
        f"issuer {provider.issuer} is not trusted for tenant {tenant!r} (allowed: {list(allowed)})"
    )

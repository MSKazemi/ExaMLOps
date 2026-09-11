"""Claim mapping: from what a center's IdP asserts to what ExaMLOps understands (ADR 0120).

Centers speak different dialects for the same fact ("this person may administer ML models"):

* Keycloak — ``groups: ["/examlops/admins"]``, ``realm_access.roles``, ``resource_access.<client>.roles``
* LDAP-backed IdPs — group DNs, ``cn=examlops-admins,ou=groups,dc=fz-juelich,dc=de``
* Research AAIs (Helmholtz ID, EGI Check-in, MyAccessID) — AARC-G069 entitlements in the
  ``entitlements`` claim (``eduperson_entitlement`` for G002 compatibility),
  ``urn:geant:helmholtz.de:group:examlops:admins:role=owner#login.helmholtz.de``
* OAuth scopes — ``scope: "openid examlops.operate"``

This module collects the candidate values from the configured claim paths, adds the normalised
forms (DN → CN, entitlement URN → group path and URN without the deprecated ``#authority``,
Keycloak path → last segment), and applies the provider's ordered role rules. The strongest
matching role wins; no match and no ``default_role`` means *authenticated but not authorized* — the
caller answers 403, never a silent viewer.

**Assurance gate.** A provider may require identity-assurance values (REFEDS RAF, carried in
``eduperson_assurance``) or an ``acr`` for a role — e.g. ``admin`` only with
``https://refeds.org/assurance/IAP/medium``. A principal whose token lacks them is capped at the
strongest role it *does* qualify for, the way JSC refuses HPC access to low-assurance logins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from examlops.iam.config import ROLE_RANK, ProviderConfig

# AARC-G069 (supersedes G002):
#   urn:<NID>:<DELEGATED-NAMESPACE>[:<SUBNAMESPACE>...]:group:<GROUP>[:<SUBGROUP>...]
#   [:role=<ROLE>][#<AUTHORITY>]
# The `#<AUTHORITY>` suffix is mandatory in G002 and deprecated-but-allowed in G069, so it is
# optional here; G069 also percent-encodes reserved characters in group names.
_AARC_RE = re.compile(
    r"^urn:(?P<nid>[^:]+):(?P<ns>.+?):group:(?P<group>[^#]+?)(?::role=(?P<role>[^#:]+))?"
    r"(?:#(?P<authority>.+))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Entitlement:
    """A parsed AARC-G069/G002 group entitlement."""

    namespace: str
    group: str  # colon-separated path, e.g. "examlops:admins"
    role: str | None
    authority: str | None


def parse_entitlement(value: str) -> Entitlement | None:
    """Parse an AARC-G069 (or legacy G002) entitlement URN, or ``None`` when it is not one."""
    m = _AARC_RE.match(value)
    if not m:
        return None
    return Entitlement(
        # G069 §3: the URN prefix (NID, namespace) is case-insensitive → normalise to lower case.
        namespace=f"{m.group('nid')}:{m.group('ns')}".lower(),
        group=unquote(m.group("group")),
        role=unquote(m.group("role")) if m.group("role") else None,
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
        if "#" in value:
            forms.append(value.split("#", 1)[0])  # G069: the #authority suffix is deprecated
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
    return list(dict.fromkeys(forms))  # de-duplicate, keeping order


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

    if provider.role_assurance:
        held = assurance_of(provider, claims)
        capped = cap_by_assurance(provider, best, held)
        if capped != best:
            matched.append(f"assurance cap: {best} → {capped or 'none'} (held: {sorted(held)})")
            best = capped
        for proj, prole in list(projects.items()):
            pcapped = cap_by_assurance(provider, prole, held)
            if pcapped is None:
                del projects[proj]
            else:
                projects[proj] = pcapped
    return RoleMapping(best, groups, projects, tuple(matched))


def assurance_of(provider: ProviderConfig, claims: dict[str, Any]) -> set[str]:
    """Assurance values a token carries: the assurance claim plus ``acr``."""
    held = set(_as_strings(get_path(claims, provider.assurance_claim)))
    if claims.get("acr") is not None:
        held.add(str(claims["acr"]))
    return held


def cap_by_assurance(provider: ProviderConfig, role: str | None, held: set[str]) -> str | None:
    """The strongest role ≤ ``role`` whose assurance requirement ``held`` satisfies."""
    if role is None:
        return None
    for candidate in sorted(ROLE_RANK, key=ROLE_RANK.__getitem__, reverse=True):
        if ROLE_RANK[candidate] > ROLE_RANK[role]:
            continue
        need = provider.role_assurance.get(candidate)
        if not need or held & set(need):
            return candidate
    return None


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

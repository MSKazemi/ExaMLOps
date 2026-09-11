"""Trust configuration: which identity providers ExaMLOps federates with, and how (ADR 0120).

A data center that already runs its own identity provider and its own authorization system is
*federated with*, never replaced. Each center is one entry in an ``identity-providers.yaml``
trust file (path in ``EXAMLOPS_IAM_CONFIG``):

.. code-block:: yaml

    providers:
      - name: jsc                                  # stable key; prefixes every principal id
        display_name: "Jülich Supercomputing Centre"
        issuer: https://login.fz-juelich.de/realms/hpc
        audience: examlops                         # RFC 9068 / RFC 8725: always bind the audience
        tenant: jsc                                # the center IS the tenant (issuer→tenant binding)
        group_claims: [groups, eduperson_entitlement]
        role_rules:
          - {value: "urn:geant:helmholtz.de:group:examlops-admins#login.helmholtz.de", role: admin}
          - {match: glob, value: "examlops-op*", role: operator}
        default_role: viewer
        authorization:
          mode: both                               # local AND the center's PDP must allow
          pdp: {type: authzen, url: https://pdp.fz-juelich.de, on_error: deny}
        clients:
          dashboard: {client_id: examlops-dashboard, client_secret_ref: "env:JSC_DASHBOARD_SECRET"}
          cli: {client_id: exa-cli}

Absent a trust file, the legacy single-issuer ``EXAMLOPS_OIDC_*`` variables still work (one
provider named ``default``). Absent both, federation is off and every service keeps its local
credentials — degrade-gracefully, like every other seam in the platform.

Validation is strict and fail-closed: an unknown key, a symmetric or ``none`` algorithm, a
plain-HTTP issuer (outside loopback), a tenant claim without an allow-list, or an unknown role is a
configuration error, and a service configured with an invalid trust file refuses federated tokens
rather than guessing.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Platform roles, weakest first. `operator` runs the lifecycle (retrain, promote, traffic) without
# the keys to the platform (secrets, config, service control), which stay with `admin`.
ROLES: tuple[str, ...] = ("viewer", "operator", "admin")
ROLE_RANK: dict[str, int] = {r: i + 1 for i, r in enumerate(ROLES)}

# Asymmetric algorithms only (RFC 8725 §3.1-3.2): a shared-secret HS* key would let every
# relying party that holds it mint tokens, and `none` is no signature at all.
ALLOWED_ALGORITHMS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256", "PS256", "ES256")

# Where group-like values are looked for when a provider does not say. Covers Keycloak
# (`groups`, `realm_access.roles`), generic OIDC (`roles`), and research AAIs that follow AARC-G002
# (`eduperson_entitlement`, `entitlements`).
DEFAULT_GROUP_CLAIMS: tuple[str, ...] = (
    "groups",
    "roles",
    "realm_access.roles",
    "eduperson_entitlement",
    "entitlements",
)

_MATCH_KINDS = ("exact", "glob", "regex")
_AUTHZ_MODES = ("local", "external", "both")
_PDP_TYPES = ("authzen", "opa")
_ON_ERROR = ("deny", "local")


class IamConfigError(ValueError):
    """The trust configuration is invalid; federated authentication is refused (fail closed)."""


@dataclass(frozen=True)
class RoleRule:
    """Map a claim value to a platform role.

    ``claim`` restricts the rule to one claim path; ``None`` means any of the provider's
    ``group_claims`` (and, for AARC-G002 entitlements, the parsed group path as well as the raw
    URN). A rule may also scope the role to named projects.
    """

    value: str
    role: str
    match: str = "exact"
    claim: str | None = None
    projects: tuple[str, ...] = ()

    def matches(self, candidate: str) -> bool:
        if self.match == "exact":
            return candidate == self.value
        if self.match == "glob":
            return fnmatch.fnmatchcase(candidate, self.value)
        return re.fullmatch(self.value, candidate) is not None


# Used when a provider declares no role rules: conventional group names and OAuth scopes.
DEFAULT_ROLE_RULES: tuple[RoleRule, ...] = (
    RoleRule("examlops-admin", "admin"),
    RoleRule("examlops-admins", "admin"),
    RoleRule("examlops-operator", "operator"),
    RoleRule("examlops-operators", "operator"),
    RoleRule("examlops-viewer", "viewer"),
    RoleRule("examlops-viewers", "viewer"),
)
DEFAULT_SCOPE_ROLES: dict[str, str] = {
    "examlops.admin": "admin",
    "examlops.operate": "operator",
    "examlops.read": "viewer",
}


@dataclass(frozen=True)
class PdpConfig:
    """The data center's own Policy Decision Point (ADR 0120 §Authorization)."""

    type: str  # authzen | opa
    url: str
    timeout_s: float = 2.0
    on_error: str = "deny"  # deny (fail closed) | local (explicit opt-in fallback)
    cache_ttl_s: float = 30.0
    token_ref: str | None = None  # bearer the PDP expects, as a secret reference
    opa_path: str = "examlops/allow"  # OPA Data API document path


@dataclass(frozen=True)
class StepUpConfig:
    """RFC 9470 step-up requirements for high-risk capabilities.

    ``enabled`` is true when the provider's entry has a ``step_up`` section: step-up is opt-in per
    center, because demanding an ``acr`` the center's IdP cannot issue would lock everyone out.
    """

    enabled: bool = False
    acr_values: tuple[str, ...] = ()
    max_age_s: int = 900


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret_ref: str | None = None
    scopes: tuple[str, ...] = ("openid", "profile", "email")


@dataclass(frozen=True)
class IntrospectionConfig:
    """RFC 7662 introspection for centers that issue opaque (non-JWT) access tokens."""

    endpoint: str | None = None  # None ⇒ taken from discovery metadata
    client_id: str = ""
    client_secret_ref: str | None = None


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    issuer: str
    display_name: str = ""
    audiences: tuple[str, ...] = ()
    jwks_uri: str | None = None
    jwks: dict[str, Any] | None = None  # inline JWKS (air-gapped centers, tests)
    discovery: bool = True
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    leeway_s: int = 60
    require_typ: bool = False  # enforce RFC 9068 `typ: at+jwt`
    subject_claim: str = "sub"
    username_claim: str = "preferred_username"
    email_claim: str = "email"
    tenant: str | None = None
    tenant_claim: str | None = None
    tenants_allowed: tuple[str, ...] = ()
    group_claims: tuple[str, ...] = DEFAULT_GROUP_CLAIMS
    role_rules: tuple[RoleRule, ...] = DEFAULT_ROLE_RULES
    scope_roles: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SCOPE_ROLES))
    default_role: str | None = None
    assurance_claim: str = "eduperson_assurance"
    role_assurance: dict[str, tuple[str, ...]] = field(default_factory=dict)
    introspection: IntrospectionConfig | None = None
    clients: dict[str, ClientConfig] = field(default_factory=dict)
    step_up: StepUpConfig = field(default_factory=StepUpConfig)
    authorization_mode: str = "local"
    pdp: PdpConfig | None = None
    allow_insecure_http: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.name

    def client(self, kind: str) -> ClientConfig | None:
        return self.clients.get(kind)


@dataclass(frozen=True)
class IamConfig:
    providers: tuple[ProviderConfig, ...] = ()
    source: str = "none"  # file:<path> | env | none

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def by_issuer(self, issuer: str) -> ProviderConfig | None:
        # Exact string comparison (OIDC Core §3.1.3.7 / RFC 8414 §3.3): no normalisation, so a
        # trailing-slash variant is a different issuer and is refused.
        for p in self.providers:
            if p.issuer == issuer:
                return p
        return None

    def by_name(self, name: str) -> ProviderConfig | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None


# ── parsing + validation ──────────────────────────────────────────────────────

_PROVIDER_KEYS = {
    "name",
    "issuer",
    "display_name",
    "audience",
    "jwks_uri",
    "jwks",
    "discovery",
    "algorithms",
    "leeway_s",
    "require_typ",
    "subject_claim",
    "username_claim",
    "email_claim",
    "tenant",
    "tenant_claim",
    "tenants_allowed",
    "group_claims",
    "role_rules",
    "scope_roles",
    "default_role",
    "introspection",
    "clients",
    "step_up",
    "assurance_claim",
    "role_assurance",
    "authorization",
    "allow_insecure_http",
}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def _check_url(what: str, url: str, allow_http: bool, errors: list[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        errors.append(f"{what}: {url!r} is not an absolute http(s) URL")
        return
    if parsed.scheme == "http" and not (allow_http or _is_loopback(url)):
        errors.append(
            f"{what}: {url!r} uses plain HTTP; tokens and keys must travel over TLS "
            "(set allow_insecure_http: true only for an isolated test network)"
        )


def _tuple(value: Any, what: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    errors.append(f"{what}: must be a string or a list of strings")
    return ()


def _parse_rules(raw: Any, where: str, errors: list[str]) -> tuple[RoleRule, ...]:
    if raw is None:
        return DEFAULT_ROLE_RULES
    if not isinstance(raw, list):
        errors.append(f"{where}.role_rules: must be a list")
        return ()
    rules: list[RoleRule] = []
    for i, r in enumerate(raw):
        at = f"{where}.role_rules[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{at}: must be a mapping")
            continue
        unknown = set(r) - {"value", "role", "match", "claim", "projects"}
        if unknown:
            errors.append(f"{at}: unknown keys {sorted(unknown)}")
        value, role = r.get("value"), r.get("role")
        match = r.get("match", "exact")
        if not isinstance(value, str) or not value:
            errors.append(f"{at}.value: required non-empty string")
            continue
        if role not in ROLES:
            errors.append(f"{at}.role: {role!r} is not one of {list(ROLES)}")
            continue
        if match not in _MATCH_KINDS:
            errors.append(f"{at}.match: {match!r} is not one of {list(_MATCH_KINDS)}")
            continue
        if match == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                errors.append(f"{at}.value: invalid regex: {exc}")
                continue
        claim = r.get("claim")
        if claim is not None and not isinstance(claim, str):
            errors.append(f"{at}.claim: must be a string")
            continue
        projects = _tuple(r.get("projects"), f"{at}.projects", errors)
        rules.append(RoleRule(value, role, match, claim, projects))
    return tuple(rules)


def _parse_pdp(raw: Any, where: str, errors: list[str], allow_http: bool) -> PdpConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    unknown = set(raw) - {
        "type",
        "url",
        "timeout_s",
        "on_error",
        "cache_ttl_s",
        "token_ref",
        "opa_path",
    }
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
    ptype, url = raw.get("type"), raw.get("url")
    if ptype not in _PDP_TYPES:
        errors.append(f"{where}.type: {ptype!r} is not one of {list(_PDP_TYPES)}")
        return None
    if not isinstance(url, str):
        errors.append(f"{where}.url: required")
        return None
    _check_url(f"{where}.url", url, allow_http, errors)
    on_error = raw.get("on_error", "deny")
    if on_error not in _ON_ERROR:
        errors.append(f"{where}.on_error: {on_error!r} is not one of {list(_ON_ERROR)}")
    try:
        timeout = float(raw.get("timeout_s", 2.0))
        ttl = float(raw.get("cache_ttl_s", 30.0))
    except (TypeError, ValueError):
        errors.append(f"{where}: timeout_s / cache_ttl_s must be numbers")
        return None
    if not 0 < timeout <= 30:
        errors.append(f"{where}.timeout_s: must be in (0, 30]")
    if ttl < 0:
        errors.append(f"{where}.cache_ttl_s: must be ≥ 0")
    return PdpConfig(
        type=ptype,
        url=url.rstrip("/"),
        timeout_s=timeout,
        on_error=on_error,
        cache_ttl_s=ttl,
        token_ref=raw.get("token_ref"),
        opa_path=str(raw.get("opa_path", "examlops/allow")).strip("/"),
    )


def _parse_provider(raw: Any, idx: int, errors: list[str]) -> ProviderConfig | None:
    where = f"providers[{idx}]"
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    unknown = set(raw) - _PROVIDER_KEYS
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append(f"{where}.name: required, lowercase [a-z0-9_-], ≤ 63 chars")
        return None
    where = f"providers[{name}]"
    issuer = raw.get("issuer")
    allow_http = bool(raw.get("allow_insecure_http", False))
    if not isinstance(issuer, str) or not issuer:
        errors.append(f"{where}.issuer: required")
        return None
    _check_url(f"{where}.issuer", issuer, allow_http, errors)

    audiences = _tuple(raw.get("audience"), f"{where}.audience", errors)
    if not audiences:
        errors.append(
            f"{where}.audience: required — without an audience check a token issued to any other "
            "application of the same center would be accepted here (RFC 8725 §3.9)"
        )

    jwks = raw.get("jwks")
    if isinstance(jwks, str):
        try:
            jwks = json.loads(jwks)
        except json.JSONDecodeError as exc:
            errors.append(f"{where}.jwks: invalid inline JSON: {exc}")
            jwks = None
    if jwks is not None and not (isinstance(jwks, dict) and isinstance(jwks.get("keys"), list)):
        errors.append(f"{where}.jwks: must be a JWKS object with a 'keys' list")
        jwks = None
    jwks_uri = raw.get("jwks_uri")
    if jwks_uri is not None:
        _check_url(f"{where}.jwks_uri", str(jwks_uri), allow_http, errors)
    discovery = bool(raw.get("discovery", jwks is None and jwks_uri is None))
    if jwks is None and jwks_uri is None and not discovery:
        errors.append(f"{where}: no key source — set jwks_uri, inline jwks, or discovery: true")

    algorithms = _tuple(raw.get("algorithms"), f"{where}.algorithms", errors) or DEFAULT_ALGORITHMS
    bad_algs = [a for a in algorithms if a not in ALLOWED_ALGORITHMS]
    if bad_algs:
        errors.append(
            f"{where}.algorithms: {bad_algs} refused — only asymmetric signatures are accepted "
            f"({sorted(ALLOWED_ALGORITHMS)})"
        )

    tenant, tenant_claim = raw.get("tenant"), raw.get("tenant_claim")
    tenants_allowed = _tuple(raw.get("tenants_allowed"), f"{where}.tenants_allowed", errors)
    if tenant and tenant_claim:
        errors.append(f"{where}: set tenant OR tenant_claim, not both")
    if tenant_claim and not tenants_allowed:
        errors.append(
            f"{where}.tenants_allowed: required with tenant_claim — an issuer may only assert the "
            "tenants it is trusted for (use ['*'] to trust it for any tenant, deliberately)"
        )
    if not tenant and not tenant_claim:
        tenant = name  # the center is its own tenant by default

    default_role = raw.get("default_role")
    if default_role is not None and default_role not in ROLES:
        errors.append(f"{where}.default_role: {default_role!r} is not one of {list(ROLES)}")

    scope_roles_raw = raw.get("scope_roles")
    scope_roles = dict(DEFAULT_SCOPE_ROLES)
    if scope_roles_raw is not None:
        if not isinstance(scope_roles_raw, dict):
            errors.append(f"{where}.scope_roles: must be a mapping scope → role")
        else:
            scope_roles = {}
            for s, r in scope_roles_raw.items():
                if r not in ROLES:
                    errors.append(f"{where}.scope_roles[{s}]: {r!r} is not one of {list(ROLES)}")
                else:
                    scope_roles[str(s)] = r

    introspection = None
    intr = raw.get("introspection")
    if intr is not None:
        if not isinstance(intr, dict) or not intr.get("client_id"):
            errors.append(f"{where}.introspection: needs at least client_id")
        else:
            ep = intr.get("endpoint")
            if ep:
                _check_url(f"{where}.introspection.endpoint", ep, allow_http, errors)
            introspection = IntrospectionConfig(
                endpoint=ep,
                client_id=intr["client_id"],
                client_secret_ref=intr.get("client_secret_ref"),
            )

    clients: dict[str, ClientConfig] = {}
    for kind, c in (raw.get("clients") or {}).items():
        if kind not in {"dashboard", "cli"}:
            errors.append(f"{where}.clients.{kind}: only 'dashboard' and 'cli' are known")
            continue
        if not isinstance(c, dict) or not c.get("client_id"):
            errors.append(f"{where}.clients.{kind}: needs client_id")
            continue
        scopes = _tuple(c.get("scopes"), f"{where}.clients.{kind}.scopes", errors) or (
            "openid",
            "profile",
            "email",
        )
        if kind == "dashboard" and not c.get("client_secret_ref"):
            # A BFF is a confidential client (draft-ietf-oauth-browser-based-apps §6.1).
            errors.append(f"{where}.clients.dashboard: client_secret_ref required (confidential)")
        clients[kind] = ClientConfig(c["client_id"], c.get("client_secret_ref"), scopes)

    role_assurance: dict[str, tuple[str, ...]] = {}
    ra = raw.get("role_assurance")
    if ra is not None:
        if not isinstance(ra, dict):
            errors.append(f"{where}.role_assurance: must be a mapping role → [assurance values]")
        else:
            for r, vals in ra.items():
                if r not in ROLES:
                    errors.append(f"{where}.role_assurance: {r!r} is not one of {list(ROLES)}")
                    continue
                role_assurance[r] = _tuple(vals, f"{where}.role_assurance.{r}", errors)

    step = raw.get("step_up") or {}
    step_up = StepUpConfig(
        enabled=bool(raw.get("step_up")),
        acr_values=_tuple(step.get("acr_values"), f"{where}.step_up.acr_values", errors),
        max_age_s=int(step.get("max_age_s", 900)),
    )

    authz = raw.get("authorization") or {}
    mode = authz.get("mode", "local")
    if mode not in _AUTHZ_MODES:
        errors.append(f"{where}.authorization.mode: {mode!r} is not one of {list(_AUTHZ_MODES)}")
    pdp = _parse_pdp(authz.get("pdp"), f"{where}.authorization.pdp", errors, allow_http)
    if mode in {"external", "both"} and pdp is None:
        errors.append(f"{where}.authorization: mode {mode!r} needs a pdp")

    try:
        leeway = int(raw.get("leeway_s", 60))
    except (TypeError, ValueError):
        errors.append(f"{where}.leeway_s: must be an integer")
        leeway = 60
    if not 0 <= leeway <= 300:
        errors.append(f"{where}.leeway_s: must be in [0, 300] seconds")

    return ProviderConfig(
        name=name,
        issuer=issuer,
        display_name=str(raw.get("display_name", "")),
        audiences=audiences,
        jwks_uri=jwks_uri,
        jwks=jwks,
        discovery=discovery,
        algorithms=algorithms,
        leeway_s=leeway,
        require_typ=bool(raw.get("require_typ", False)),
        subject_claim=str(raw.get("subject_claim", "sub")),
        username_claim=str(raw.get("username_claim", "preferred_username")),
        email_claim=str(raw.get("email_claim", "email")),
        tenant=tenant,
        tenant_claim=tenant_claim,
        tenants_allowed=tenants_allowed,
        group_claims=_tuple(raw.get("group_claims"), f"{where}.group_claims", errors)
        or DEFAULT_GROUP_CLAIMS,
        role_rules=_parse_rules(raw.get("role_rules"), where, errors),
        scope_roles=scope_roles,
        default_role=default_role,
        introspection=introspection,
        clients=clients,
        step_up=step_up,
        assurance_claim=str(raw.get("assurance_claim", "eduperson_assurance")),
        role_assurance=role_assurance,
        authorization_mode=mode,
        pdp=pdp,
        allow_insecure_http=allow_http,
    )


def parse_config(data: Any, *, source: str = "inline") -> tuple[IamConfig, list[str]]:
    """Parse a trust document into an :class:`IamConfig` plus a list of validation errors."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return IamConfig(source=source), ["trust file must be a mapping with a 'providers' list"]
    unknown = set(data) - {"providers", "version"}
    if unknown:
        errors.append(f"unknown top-level keys {sorted(unknown)}")
    raw_providers = data.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        return IamConfig(source=source), errors + ["'providers' must be a non-empty list"]
    providers: list[ProviderConfig] = []
    for i, raw in enumerate(raw_providers):
        p = _parse_provider(raw, i, errors)
        if p is not None:
            providers.append(p)
    names = [p.name for p in providers]
    issuers = [p.issuer for p in providers]
    for n in {n for n in names if names.count(n) > 1}:
        errors.append(f"duplicate provider name {n!r}")
    for iss in {i for i in issuers if issuers.count(i) > 1}:
        errors.append(f"duplicate issuer {iss!r} — one issuer maps to exactly one provider")
    return IamConfig(tuple(providers), source), errors


def _legacy_env_config() -> IamConfig | None:
    """The single-issuer ``EXAMLOPS_OIDC_*`` configuration (Phase 2 item 2.1), if set."""
    issuer = os.getenv("EXAMLOPS_OIDC_ISSUER", "").strip()
    if not issuer:
        return None
    jwks_raw = os.getenv("EXAMLOPS_OIDC_JWKS", "").strip()
    jwks: dict[str, Any] | None = None
    jwks_uri: str | None = None
    if jwks_raw.startswith(("http://", "https://")):
        jwks_uri = jwks_raw
    elif jwks_raw:
        try:
            jwks = json.loads(jwks_raw)
        except json.JSONDecodeError:
            jwks = None
    audience = os.getenv("EXAMLOPS_OIDC_AUDIENCE", "").strip()
    default_role = os.getenv("EXAMLOPS_OIDC_DEFAULT_ROLE", "").strip() or None
    tenant_claim = os.getenv("EXAMLOPS_OIDC_TENANT_CLAIM", "tenant").strip() or "tenant"
    provider = ProviderConfig(
        name="default",
        issuer=issuer,
        audiences=(audience,) if audience else (),
        jwks_uri=jwks_uri,
        jwks=jwks,
        discovery=jwks is None and jwks_uri is None,
        algorithms=("RS256",),
        subject_claim=os.getenv("EXAMLOPS_OIDC_SUBJECT_CLAIM", "sub").strip() or "sub",
        tenant_claim=tenant_claim,
        tenants_allowed=("*",),
        default_role=default_role if default_role in ROLES else None,
        allow_insecure_http=True,  # legacy mode kept its original (unchecked) behaviour
    )
    return IamConfig((provider,), "env")


def config_path() -> Path | None:
    raw = os.getenv("EXAMLOPS_IAM_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else None


def load_file(path: Path) -> tuple[IamConfig, list[str]]:
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return IamConfig(source=f"file:{path}"), [f"cannot read trust file {path}: {exc}"]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return IamConfig(source=f"file:{path}"), [f"trust file {path} is not valid YAML: {exc}"]
    return parse_config(data, source=f"file:{path}")


_cache: dict[str, tuple[float, IamConfig, tuple[str, ...]]] = {}


def load_config(*, strict: bool = True) -> IamConfig:
    """The effective trust configuration (file → legacy env → disabled).

    With ``strict`` (the default for every enforcement point) an invalid trust file raises
    :class:`IamConfigError`, so a service refuses federated tokens rather than trusting a
    half-parsed file. The parse is cached per file mtime.
    """
    path = config_path()
    if path is not None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = -1.0
        key = str(path)
        hit = _cache.get(key)
        if hit is not None and hit[0] == mtime:
            cfg, errors = hit[1], hit[2]
        else:
            cfg, errs = load_file(path)
            errors = tuple(errs)
            _cache[key] = (mtime, cfg, errors)
        if errors and strict:
            raise IamConfigError("; ".join(errors))
        return cfg
    legacy = _legacy_env_config()
    return legacy if legacy is not None else IamConfig()


def clear_cache() -> None:
    _cache.clear()


def resolve_secret_ref(ref: str | None) -> str | None:
    """Resolve ``env:NAME`` or ``secret:<path>`` (the D7 secret store). Never logs the value."""
    if not ref:
        return None
    if ref.startswith("env:"):
        return os.getenv(ref[4:]) or None
    if ref.startswith("secret:"):
        try:
            from examlops.secrets import get_secret

            return get_secret(ref[7:]) or None
        except Exception:  # noqa: BLE001 — an unreachable store means "no secret", fail closed
            return None
    raise IamConfigError(f"secret reference {ref!r} must start with 'env:' or 'secret:'")

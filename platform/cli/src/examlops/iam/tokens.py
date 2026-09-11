"""Access-token verification for every ExaMLOps resource server (ADR 0120).

One verifier for the dashboard BFF, the control plane and anything else that accepts a bearer:

1. **Issuer selection before trust.** The unverified ``iss`` only *selects* a configured provider
   by exact match; an unknown issuer is refused. Each provider brings its own keys, audience,
   algorithms and claim mapping, so two centers can never vouch for each other's users (multi-issuer
   confusion defence, RFC 9700 §4.4 / RFC 8725 §3.8).
2. **Signature with an allow-listed asymmetric algorithm** (RFC 8725 §3.1): the header ``alg`` must
   be in the provider's list and match the JWK's key type; ``none``/HS* never verify.
3. **Claims** — ``iss`` exact, ``aud`` intersects the provider's audiences (RFC 8725 §3.9), ``exp``
   / ``nbf`` / ``iat`` with bounded leeway, optional RFC 9068 ``typ: at+jwt``.
4. **Opaque tokens** go to RFC 7662 introspection — only at the provider the caller names (or the
   only provider configured for introspection), because sending a token to the *wrong* center's
   introspection endpoint would disclose it.
5. **Mapping** — subject is ``(provider, sub)`` (OIDC Core §5.7: only ``iss``+``sub`` is a stable
   identifier), tenant via the issuer→tenant binding, role via the provider's rules.

Every failure raises :class:`AuthenticationError`; nothing here ever returns a partially verified
principal.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from examlops.iam import metadata
from examlops.iam.claims import TenantError, get_path, map_roles, resolve_tenant, scopes_of
from examlops.iam.config import (
    ROLE_RANK,
    IamConfig,
    IamConfigError,
    ProviderConfig,
    load_config,
    resolve_secret_ref,
)


class AuthenticationError(RuntimeError):
    """The bearer could not be authenticated (HTTP 401). ``reason`` is safe to return."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    """A verified, federated caller."""

    provider: str
    issuer: str
    subject: str
    tenant: str
    role: str | None
    username: str = ""
    email: str = ""
    groups: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    projects: dict[str, str] = field(default_factory=dict)
    acr: str | None = None
    amr: tuple[str, ...] = ()
    auth_time: int | None = None
    expires_at: int | None = None
    token_type: str = "jwt"  # jwt | opaque
    matched: tuple[str, ...] = ()
    claims: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        """Stable identifier — ``<provider>:<sub>``. Use this for grants and joins."""
        return f"{self.provider}:{self.subject}"

    @property
    def actor(self) -> str:
        """Human-readable audit actor — ``<provider>:<username or sub>``."""
        return f"{self.provider}:{self.username or self.subject}"

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK.get(self.role or "", 0) >= ROLE_RANK.get(minimum, 99)

    def summary(self) -> dict[str, Any]:
        """A JSON-safe view without the raw claims (safe to print / return from ``whoami``)."""
        return {
            "id": self.id,
            "actor": self.actor,
            "provider": self.provider,
            "issuer": self.issuer,
            "subject": self.subject,
            "username": self.username,
            "email": self.email,
            "tenant": self.tenant,
            "role": self.role,
            "projects": dict(self.projects),
            "groups": list(self.groups),
            "scopes": list(self.scopes),
            "acr": self.acr,
            "amr": list(self.amr),
            "auth_time": self.auth_time,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "matched": list(self.matched),
        }


def looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts[:2])


def _config(config: IamConfig | None) -> IamConfig:
    if config is not None:
        return config
    try:
        return load_config()
    except IamConfigError as exc:
        raise AuthenticationError(f"identity federation misconfigured: {exc}") from exc


def _select_key(provider: ProviderConfig, header: dict[str, Any]):
    import jwt

    kid = header.get("kid")
    alg = header.get("alg")

    def _find(keyset: dict[str, Any]):
        candidates = [
            k
            for k in keyset.get("keys", [])
            if isinstance(k, dict) and k.get("use", "sig") == "sig"
        ]
        if kid is not None:
            candidates = [k for k in candidates if k.get("kid") == kid]
        elif len(candidates) != 1:
            return None  # no kid and several keys: ambiguous, refuse rather than try each
        for k in candidates:
            if k.get("alg") not in (None, alg):
                continue
            try:
                return jwt.PyJWK(k, algorithm=alg)
            except jwt.PyJWTError:
                continue
        return None

    try:
        found = _find(metadata.jwks(provider))
        if found is None:
            # Unknown kid: the center may have rotated its keys. Refetch once (rate-limited).
            found = _find(metadata.jwks(provider, force=True))
    except metadata.MetadataError as exc:
        raise AuthenticationError(f"cannot obtain signing keys for {provider.name}: {exc}") from exc
    if found is None:
        raise AuthenticationError(f"no signing key of provider {provider.name!r} matches kid={kid}")
    return found


def principal_from_claims(
    provider: ProviderConfig, claims: dict[str, Any], token_type: str
) -> Principal:
    """Map *already verified* claims from ``provider`` to a :class:`Principal`.

    Callers must have verified the claims themselves (a JWT via :func:`verify_jwt`, an ID token via
    ``flows.verify_id_token``, an introspection response); this function only maps.
    """
    subject = get_path(claims, provider.subject_claim)
    if not isinstance(subject, (str, int)) or not str(subject):
        raise AuthenticationError(f"token has no subject claim {provider.subject_claim!r}")
    try:
        tenant = resolve_tenant(provider, claims)
    except TenantError as exc:
        raise AuthenticationError(str(exc)) from exc
    mapping = map_roles(provider, claims)
    amr = claims.get("amr")
    auth_time = claims.get("auth_time")
    return Principal(
        provider=provider.name,
        issuer=provider.issuer,
        subject=str(subject),
        tenant=tenant,
        role=mapping.role,
        username=str(get_path(claims, provider.username_claim) or ""),
        email=str(get_path(claims, provider.email_claim) or ""),
        groups=mapping.groups,
        scopes=scopes_of(claims),
        projects=mapping.projects,
        acr=str(claims["acr"]) if claims.get("acr") is not None else None,
        amr=tuple(str(a) for a in amr) if isinstance(amr, list) else (),
        auth_time=int(auth_time) if isinstance(auth_time, (int, float)) else None,
        expires_at=int(claims["exp"]) if isinstance(claims.get("exp"), (int, float)) else None,
        token_type=token_type,
        matched=mapping.matched,
        claims=claims,
    )


def verify_jwt(
    token: str,
    config: IamConfig | None = None,
    *,
    audience: tuple[str, ...] | None = None,
) -> tuple[ProviderConfig, dict[str, Any]]:
    """Verify a signed JWT from a trusted issuer; return ``(provider, claims)``.

    ``audience`` overrides the provider's resource audiences — the dashboard uses it to verify an
    ID token, whose audience is the dashboard's client id.
    """
    import jwt

    cfg = _config(config)
    if not cfg.enabled:
        raise AuthenticationError("identity federation is not configured")
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"malformed token: {exc}") from exc
    issuer = unverified.get("iss")
    if not isinstance(issuer, str):
        raise AuthenticationError("token has no issuer")
    provider = cfg.by_issuer(issuer)
    if provider is None:
        raise AuthenticationError(f"issuer {issuer!r} is not a trusted identity provider")
    alg = header.get("alg")
    if alg not in provider.algorithms:
        raise AuthenticationError(
            f"algorithm {alg!r} not accepted for {provider.name} (allowed: {list(provider.algorithms)})"
        )
    if provider.require_typ and str(header.get("typ", "")).lower() not in {
        "at+jwt",
        "application/at+jwt",
    }:
        raise AuthenticationError("access token must be typed 'at+jwt' (RFC 9068 §2.1)")
    key = _select_key(provider, header)
    auds = audience if audience is not None else provider.audiences
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=list(auds) if auds else None,
            issuer=provider.issuer,
            leeway=provider.leeway_s,
            options={"require": ["exp", "iss"], "verify_aud": bool(auds)},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"token verification failed: {exc}") from exc
    return provider, claims


# ── RFC 7662 introspection ────────────────────────────────────────────────────

_intro_lock = threading.Lock()
_intro_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}


def _introspection_provider(cfg: IamConfig, hint: str | None) -> ProviderConfig:
    if hint:
        p = cfg.by_name(hint)
        if p is None or p.introspection is None:
            raise AuthenticationError(f"provider {hint!r} is not configured for opaque tokens")
        return p
    able = [p for p in cfg.providers if p.introspection is not None]
    if len(able) == 1:
        return able[0]
    if not able:
        raise AuthenticationError("opaque tokens are not accepted (no introspection configured)")
    raise AuthenticationError(
        "opaque token is ambiguous across providers; name the provider "
        "(header X-ExaMLOps-IdP) so the token is only ever sent to its own issuer"
    )


def introspect(token: str, config: IamConfig | None = None, *, provider_hint: str | None = None):
    """Validate an opaque token at its issuer's introspection endpoint (RFC 7662)."""
    import httpx

    cfg = _config(config)
    provider = _introspection_provider(cfg, provider_hint)
    intr = provider.introspection
    assert intr is not None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _intro_lock:
        hit = _intro_cache.get(digest)
        if hit and hit[0] > now and hit[1] == provider.name:
            return provider, hit[2]
    try:
        url = intr.endpoint or metadata.endpoint(provider, "introspection_endpoint")
    except metadata.MetadataError as exc:
        raise AuthenticationError(str(exc)) from exc
    secret = resolve_secret_ref(intr.client_secret_ref) or ""
    try:
        resp = httpx.post(
            url,
            data={"token": token, "token_type_hint": "access_token"},
            auth=(intr.client_id, secret),
            headers={"Accept": "application/json"},
            timeout=metadata.http_timeout(),
        )
    except httpx.HTTPError as exc:
        raise AuthenticationError(f"introspection at {provider.name} unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise AuthenticationError(f"introspection at {provider.name} answered {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict) or data.get("active") is not True:
        raise AuthenticationError("token is not active")
    if data.get("iss") not in (None, provider.issuer):
        raise AuthenticationError("introspection response names a different issuer")
    exp = data.get("exp")
    if isinstance(exp, (int, float)) and exp + provider.leeway_s < now:
        raise AuthenticationError("token expired")
    if provider.audiences:
        aud = data.get("aud")
        auds = {aud} if isinstance(aud, str) else set(aud or [])
        if not auds & set(provider.audiences):
            raise AuthenticationError("token audience does not include this service")
    data.setdefault("iss", provider.issuer)
    ttl = 30.0 if not isinstance(exp, (int, float)) else max(0.0, min(30.0, exp - now))
    with _intro_lock:
        if len(_intro_cache) > 10_000:
            _intro_cache.clear()
        _intro_cache[digest] = (now + ttl, provider.name, data)
    return provider, data


def verify_access_token(
    token: str, config: IamConfig | None = None, *, provider_hint: str | None = None
) -> Principal:
    """Verify a bearer access token (JWT or opaque) and return the federated :class:`Principal`."""
    token = token.strip()
    if not token:
        raise AuthenticationError("empty bearer token")
    if looks_like_jwt(token):
        provider, claims = verify_jwt(token, config)
        return principal_from_claims(provider, claims, "jwt")
    provider, claims = introspect(token, config, provider_hint=provider_hint)
    return principal_from_claims(provider, claims, "opaque")


def verify_bearer(
    authorization_header: str | None,
    config: IamConfig | None = None,
    *,
    provider_hint: str | None = None,
) -> Principal:
    """Verify an ``Authorization: Bearer <token>`` header value."""
    if not authorization_header:
        raise AuthenticationError("missing Authorization header")
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError("Authorization header must be 'Bearer <token>'")
    return verify_access_token(parts[1], config, provider_hint=provider_hint)


def clear_cache() -> None:
    with _intro_lock:
        _intro_cache.clear()

"""OIDC/OAuth2 access-token validation + identity propagation (Phase 2 item 2.1).

Enterprise SSO: instead of the shared control-plane token / HS256 dashboard secret, accept
IdP-issued **RS256** access tokens, verify them against the issuer's JWKS, and derive a real
per-user **subject** + **tenant** to thread into `EXAMLOPS_ACTOR` and the audit trail. This is the
verification core; a route dependency (control plane / dashboard / Skipper) calls
:func:`verify_bearer` on the ``Authorization: Bearer …`` header and gets back an :class:`Identity`.

Configured entirely by env, so it's off by default (single-tenant/dev keeps working) and turns on
when an issuer is set — degrade-gracefully, same as every other seam. JWKS can be a URL (fetched +
cached by PyJWT) or inline JSON (air-gapped / tests).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


class OidcError(RuntimeError):
    """Raised when a token is missing, malformed, or fails verification (fail-closed)."""


class OidcNotConfigured(RuntimeError):
    """Raised when OIDC verification is attempted but no issuer is configured."""


@dataclass(frozen=True)
class Identity:
    """A verified caller identity derived from an OIDC token."""

    subject: str
    tenant: str
    scopes: tuple[str, ...] = ()
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def actor(self) -> str:
        """The value to write into ``EXAMLOPS_ACTOR`` / audit — ``tenant/subject``."""
        return f"{self.tenant}/{self.subject}" if self.tenant != "default" else self.subject


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_enabled() -> bool:
    """True when an OIDC issuer is configured (``EXAMLOPS_OIDC_ISSUER``)."""
    return bool(_env("EXAMLOPS_OIDC_ISSUER"))


def _tenant_claim() -> str:
    return _env("EXAMLOPS_OIDC_TENANT_CLAIM", "tenant") or "tenant"


def _subject_claim() -> str:
    return _env("EXAMLOPS_OIDC_SUBJECT_CLAIM", "sub") or "sub"


def _load_jwks(jwks: dict | str | None):
    """Return a PyJWT key resolver from inline JWKS/dict or the configured JWKS URL."""
    import jwt

    if jwks is None:
        jwks = _env("EXAMLOPS_OIDC_JWKS")
    if not jwks:
        raise OidcNotConfigured("no JWKS: set EXAMLOPS_OIDC_JWKS (URL or inline JSON)")
    if isinstance(jwks, str) and jwks.startswith(("http://", "https://")):
        return jwt.PyJWKClient(jwks)
    data = json.loads(jwks) if isinstance(jwks, str) else jwks
    return jwt.PyJWKSet.from_dict(data)


def _signing_key(token: str, keyset):
    import jwt

    if isinstance(keyset, jwt.PyJWKClient):
        return keyset.get_signing_key_from_jwt(token).key
    # PyJWKSet: match on the token's kid.
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    for k in keyset.keys:
        if kid is None or k.key_id == kid:
            return k.key
    raise OidcError(f"no JWKS key matches token kid={kid}")


def verify_token(token: str, *, jwks: dict | str | None = None) -> Identity:
    """Verify an OIDC access token and return the caller :class:`Identity` (fail-closed).

    Checks the RS256 signature against the JWKS, plus issuer/audience/expiry, then extracts the
    subject + tenant claims. Raises :class:`OidcError` on any failure — never returns a partial or
    unverified identity.
    """
    import jwt

    if not is_enabled():
        raise OidcNotConfigured("OIDC disabled — set EXAMLOPS_OIDC_ISSUER to enable SSO")
    issuer = _env("EXAMLOPS_OIDC_ISSUER")
    audience = _env("EXAMLOPS_OIDC_AUDIENCE") or None
    try:
        key = _signing_key(token, _load_jwks(jwks))
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss"], "verify_aud": audience is not None},
        )
    except OidcError:
        raise
    except jwt.PyJWTError as exc:
        raise OidcError(f"token verification failed: {exc}") from exc

    subject = claims.get(_subject_claim())
    if not subject:
        raise OidcError(f"token missing subject claim '{_subject_claim()}'")
    tenant = str(claims.get(_tenant_claim()) or "default")
    scope = claims.get("scope") or claims.get("scp") or ""
    scopes = tuple(scope.split()) if isinstance(scope, str) else tuple(scope)
    return Identity(subject=str(subject), tenant=tenant, scopes=scopes, claims=claims)


def verify_bearer(authorization_header: str | None, *, jwks: dict | str | None = None) -> Identity:
    """Verify an ``Authorization: Bearer <token>`` header. Raises :class:`OidcError` if absent/bad."""
    if not authorization_header:
        raise OidcError("missing Authorization header")
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise OidcError("Authorization header must be 'Bearer <token>'")
    return verify_token(parts[1], jwks=jwks)

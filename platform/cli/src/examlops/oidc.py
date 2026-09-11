"""Single-issuer OIDC token validation — compatibility surface (Phase 2 item 2.1).

Superseded by :mod:`examlops.iam` (ADR 0120), which federates with *several* data-center identity
providers, maps their groups/entitlements to platform roles, binds each issuer to its tenant and
delegates authorization to the center's PDP. This module keeps the original single-issuer API
(``EXAMLOPS_OIDC_ISSUER`` / ``_AUDIENCE`` / ``_JWKS`` / ``_TENANT_CLAIM`` / ``_SUBJECT_CLAIM``) and
now verifies through the same code path as every service, so there is exactly one token verifier
in the platform.
"""

from __future__ import annotations

import dataclasses
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


def is_enabled() -> bool:
    """True when an OIDC issuer is configured (``EXAMLOPS_OIDC_ISSUER``)."""
    return bool(os.getenv("EXAMLOPS_OIDC_ISSUER", "").strip())


def verify_token(token: str, *, jwks: dict | str | None = None) -> Identity:
    """Verify an OIDC access token and return the caller :class:`Identity` (fail-closed).

    Checks the RS256 signature against the JWKS, plus issuer/audience/expiry, then extracts the
    subject + tenant claims. Raises :class:`OidcError` on any failure — never returns a partial or
    unverified identity.
    """
    from examlops.iam.config import IamConfig, _legacy_env_config
    from examlops.iam.tokens import AuthenticationError, verify_jwt

    cfg = _legacy_env_config()
    if cfg is None:
        raise OidcNotConfigured("OIDC disabled — set EXAMLOPS_OIDC_ISSUER to enable SSO")
    provider = cfg.providers[0]
    if jwks is not None:
        if isinstance(jwks, str) and jwks.startswith(("http://", "https://")):
            provider = dataclasses.replace(provider, jwks=None, jwks_uri=jwks, discovery=False)
        else:
            data = json.loads(jwks) if isinstance(jwks, str) else jwks
            provider = dataclasses.replace(provider, jwks=data, jwks_uri=None, discovery=False)
    elif provider.jwks is None and provider.jwks_uri is None:
        raise OidcNotConfigured("no JWKS: set EXAMLOPS_OIDC_JWKS (URL or inline JSON)")
    try:
        _, claims = verify_jwt(token, IamConfig((provider,), "env"))
    except AuthenticationError as exc:
        raise OidcError(f"token verification failed: {exc.reason}") from exc

    subject = claims.get(provider.subject_claim)
    if not subject:
        raise OidcError(f"token missing subject claim '{provider.subject_claim}'")
    tenant = str(claims.get(provider.tenant_claim or "tenant") or "default")
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

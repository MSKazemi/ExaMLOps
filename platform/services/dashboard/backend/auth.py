"""Auth utilities: password compare, session JWT issue/verify, role-gate dependency.

Three ways to be authenticated (ADR 0057, ADR 0120):

1. **Organisation SSO (preferred).** The BFF runs Authorization Code + PKCE against the data
   center's IdP (``routers/sso.py``) and holds the session in an ``HttpOnly; Secure;
   SameSite=Strict`` cookie — no IdP token and no session token ever reaches browser JavaScript
   (RFC 10017, the BFF pattern). A cookie-authenticated state-changing request must also prove it is
   same-origin (``Sec-Fetch-Site``/``Origin``, or the ``X-ExaMLOps-CSRF`` header the SPA sends).
2. **IdP bearer tokens** for API clients and scripts: an access token from a trusted center
   (``examlops.iam``) in ``Authorization: Bearer`` — verified, role-mapped, never stored.
3. **Local break-glass passwords** (viewer/admin, the original shared-password login). Kept for
   bootstrap and outage recovery; an enterprise deployment turns them off with
   ``DASHBOARD_LOCAL_LOGIN=false`` once SSO works.
"""

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from settings import settings

Role = Literal["viewer", "operator", "admin"]
# `operator` (ADR 0120) runs the model lifecycle — retrain, promote, approvals, traffic — without
# holding the keys to the platform (secrets, config, service control), which stay with `admin`.
_ROLE_RANK: dict[str, int] = {"viewer": 1, "operator": 2, "admin": 3}

_bearer = HTTPBearer(auto_error=False)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-ExaMLOps-CSRF"


def session_cookie_name() -> str:
    # `__Host-` binds the cookie to this exact origin (Secure, Path=/, no Domain) — RFC 10017 §6.1.
    # Browsers refuse a Secure cookie over plain HTTP except on localhost, so a deployment reached
    # over http://<ip> must set DASHBOARD_SESSION_COOKIE_SECURE=false (and then loses the prefix).
    return (
        "__Host-examlops_session"
        if settings.dashboard_session_cookie_secure
        else "examlops_session"
    )


# Placeholder markers, matched as substrings so that a *decorated* example value
# (`change-me-admin`, not the bare word `changeme`) is caught too. Same list as the
# control plane's `_WEAK_MARKERS`; the two services share no import path, so it is
# duplicated rather than imported. Every marker is long enough not to occur by
# accident inside a generated secret.
_WEAK_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "changethis",
    "placeholder",
    "your-token",
    "yourtoken",
    "replace-me",
    "replaceme",
)


def is_placeholder(value: str) -> bool:
    """True when a configured credential is one of the shipped example values.

    `.env.example` has to show *something* next to each variable, and whatever it
    shows will be copied into a real `.env` by someone in a hurry. Requiring the
    variable to be set is not enough — it was set, to the published value. So a
    credential carrying a placeholder marker is treated as not configured.
    """
    return any(m in value.lower() for m in _WEAK_MARKERS)


def check_password(plaintext: str) -> Role | None:
    """Return the matching role, or None on miss. Constant-time compare;
    viewer checked first so no timing distinction between roles.

    A role whose configured password is a shipped placeholder can never be logged
    into: the compare still runs (so the timing shape is unchanged), but a match
    against a placeholder is refused. Which role is misconfigured is not a secret —
    the password it would accept is published in `.env.example`.
    """
    if not plaintext:
        return None
    viewer_ok = secrets.compare_digest(plaintext, settings.dashboard_viewer_password)
    admin_ok = secrets.compare_digest(plaintext, settings.dashboard_admin_password)
    if viewer_ok and not is_placeholder(settings.dashboard_viewer_password):
        return "viewer"
    if admin_ok and not is_placeholder(settings.dashboard_admin_password):
        return "admin"
    return None


def issue_token(
    role: Role,
    *,
    identity: dict[str, Any] | None = None,
    ttl: timedelta | None = None,
) -> tuple[str, datetime]:
    """Create a session JWT for the given role; return (token, expires_at_utc).

    Without ``identity`` (password login) ``sub`` is a stable per-session actor id
    (``<role>@<jti prefix>``) so audit events record who acted instead of "?" — shared-password auth
    has no username to use (D6). With ``identity`` (SSO) the session carries the federated
    principal: ``sub`` = ``<provider>:<sub>``, tenant, IdP, display name, groups, and the
    ``acr``/``amr``/``auth_time`` that step-up (RFC 9470) is evaluated against.
    """
    now = datetime.now(UTC)
    expires_at = now + (ttl or timedelta(hours=settings.dashboard_jwt_ttl_hours))
    jti = secrets.token_hex(16)
    claims: dict[str, Any] = {
        "sub": f"{role}@{jti[:8]}",
        "role": role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "auth_time": int(now.timestamp()),
        "amr": ["pwd"],
        "exp": int(expires_at.timestamp()),
    }
    if identity:
        claims.update({k: v for k, v in identity.items() if v is not None})
        if identity.get("amr") is None:
            claims.pop("amr")  # the IdP did not say how the user authenticated; do not guess "pwd"
        claims["role"] = role
        claims["jti"] = jti
        claims["exp"] = int(expires_at.timestamp())
    token = jwt.encode(claims, settings.dashboard_jwt_secret, algorithm="HS256")
    return token, expires_at


def verify_token(token: str) -> dict:
    """Decode + verify a dashboard session token; raise 401 on any failure."""
    try:
        return jwt.decode(token, settings.dashboard_jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _role_at_least(actual: str, minimum: str) -> bool:
    return _ROLE_RANK.get(actual, 0) >= _ROLE_RANK.get(minimum, 99)


def _is_session_token(token: str) -> bool:
    try:
        return jwt.get_unverified_header(token).get("alg") == "HS256"
    except jwt.PyJWTError:
        return False


# The session is a cookie, and browsers silently drop cookies over ~4 KB — a user with many AARC
# entitlements would log in and bounce straight back to the login page. Keep the group list (only
# used as PDP context; roles are already resolved) inside a fixed budget.
_GROUPS_BUDGET_BYTES = 1500


def _bounded_groups(groups: Any) -> list[str]:
    out: list[str] = []
    used = 0
    for g in groups:
        size = len(str(g).encode()) + 3
        if used + size > _GROUPS_BUDGET_BYTES:
            break
        out.append(str(g))
        used += size
    return out


def claims_from_principal(principal: Any, *, via: str) -> dict[str, Any]:
    """Shape a verified ``examlops.iam.Principal`` into dashboard session claims."""
    return {
        "sub": principal.id,
        "name": principal.username or principal.email or principal.subject,
        "email": principal.email or None,
        "role": principal.role,
        "tenant": principal.tenant,
        "idp": principal.provider,
        "iss_idp": principal.issuer,
        "idp_sub": principal.subject,
        "groups": _bounded_groups(principal.groups),
        "projects": dict(principal.projects) or None,
        "acr": principal.acr,
        "amr": list(principal.amr) or None,
        "auth_time": principal.auth_time,
        "via": via,
    }


def _idp_bearer_claims(token: str) -> dict:
    """Verify an access token from a trusted data-center IdP (API clients, scripts)."""
    try:
        from examlops import iam  # type: ignore
    except ImportError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc
    if not iam.is_enabled():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = iam.verify_access_token(token)
    except iam.AuthenticationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"invalid token: {exc.reason}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    if principal.role is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Authenticated as {principal.actor}, but your identity provider grants no ExaMLOps role",
        )
    claims = claims_from_principal(principal, via="idp-bearer")
    claims["exp"] = principal.expires_at or int(time.time()) + 60
    return claims


def _still_trusted(claims: dict) -> dict:
    """A federated session dies the moment its center leaves the trust file (ADR 0120).

    Removing a provider from ``identity-providers.yaml`` is how an operator cuts a center off; its
    users' existing sessions must not outlive that decision by the session TTL.
    """
    if claims.get("via") != "sso" or not claims.get("idp"):
        return claims
    from iam_gate import provider_for  # noqa: PLC0415 — iam_gate imports capabilities → auth

    if provider_for(claims) is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "your identity provider is no longer trusted by this platform",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


def _same_origin(request: Request) -> bool:
    """CSRF defence for cookie sessions (OWASP: verify origin with standard headers)."""
    if request.headers.get(CSRF_HEADER):
        return True  # a custom header cannot be set cross-origin without a CORS grant
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("origin")
    if origin is None:
        return False
    from urllib.parse import urlparse

    return urlparse(origin).netloc == request.headers.get("host", "")


def authenticate(request: Request | None, credentials: HTTPAuthorizationCredentials | None) -> dict:
    """Resolve the caller's claims from a bearer token or the SSO session cookie (or 401)."""
    if credentials is not None and credentials.credentials:
        token = credentials.credentials
        if _is_session_token(token):
            return _still_trusted(verify_token(token))
        return _idp_bearer_claims(token)
    cookie = request.cookies.get(session_cookie_name()) if request is not None else None
    if cookie:
        claims = _still_trusted(verify_token(cookie))
        if (
            request is not None
            and request.method not in _SAFE_METHODS
            and not _same_origin(request)
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused (CSRF)")
        return claims
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_role(min_role: Role):
    """FastAPI dependency factory. Use as ``Depends(require_role("admin"))``."""

    def _dep(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    ) -> dict:
        payload = authenticate(request, credentials)
        if not _role_at_least(payload.get("role", ""), min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{min_role}' required",
            )
        if payload.get("idp"):
            # A data center's user: its own PDP may veto this route (ADR 0120).
            from iam_gate import center_route_check  # noqa: PLC0415 — circular at import time

            center_route_check(payload, request)
        return payload

    return _dep

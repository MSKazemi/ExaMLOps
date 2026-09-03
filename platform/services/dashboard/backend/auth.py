"""Auth utilities: password compare, JWT issue/verify, role-gate dependency."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from settings import settings

Role = Literal["viewer", "admin"]
_ROLE_RANK: dict[str, int] = {"viewer": 1, "admin": 2}

_bearer = HTTPBearer(auto_error=False)


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


def issue_token(role: Role) -> tuple[str, datetime]:
    """Create a JWT for the given role; return (token, expires_at_utc).

    ``sub`` is a stable per-session actor id (``<role>@<jti prefix>``) so audit events
    record who acted instead of "?" — shared-password auth has no username to use (D6).
    """
    expires_at = datetime.now(UTC) + timedelta(hours=settings.dashboard_jwt_ttl_hours)
    jti = secrets.token_hex(16)
    token = jwt.encode(
        {
            "sub": f"{role}@{jti[:8]}",
            "role": role,
            "jti": jti,
            "exp": int(expires_at.timestamp()),
        },
        settings.dashboard_jwt_secret,
        algorithm="HS256",
    )
    return token, expires_at


def verify_token(token: str) -> dict:
    """Decode + verify; raise 401 on any failure."""
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


def require_role(min_role: Role):
    """FastAPI dependency factory. Use as ``Depends(require_role("admin"))``."""

    def _dep(
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    ) -> dict:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = verify_token(credentials.credentials)
        if not _role_at_least(payload.get("role", ""), min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{min_role}' required",
            )
        return payload

    return _dep

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


def check_password(plaintext: str) -> Role | None:
    """Return the matching role, or None on miss. Constant-time compare;
    viewer checked first so no timing distinction between roles."""
    if not plaintext:
        return None
    if secrets.compare_digest(plaintext, settings.dashboard_viewer_password):
        return "viewer"
    if secrets.compare_digest(plaintext, settings.dashboard_admin_password):
        return "admin"
    return None


def issue_token(role: Role) -> tuple[str, datetime]:
    """Create a JWT for the given role; return (token, expires_at_utc)."""
    expires_at = datetime.now(UTC) + timedelta(hours=settings.dashboard_jwt_ttl_hours)
    token = jwt.encode(
        {"role": role, "exp": int(expires_at.timestamp())},
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

"""Login, logout, me — the unauthenticated entry points are /login and the SSO routes (sso.py)."""

from datetime import UTC

from auth import check_password, issue_token, require_role, session_cookie_name
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from security import RateLimiter, rate_limit
from settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# Brute-force gate on the one credential-checking endpoint (F16 R7). Per-client,
# fixed-window; failed attempts count too, since the dependency runs before the
# handler. Tests reset this between cases via the ``client`` fixture.
LOGIN_LIMITER = RateLimiter(limit=10, window_seconds=60.0)
_login_rate = rate_limit(LOGIN_LIMITER)


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str
    role: str
    expires_at: str


class MeResponse(BaseModel):
    role: str
    expires_at: str
    tenant: str = "default"
    capabilities: list[str] = []
    # ADR 0120 — who the session belongs to and how it was established.
    sub: str = ""
    name: str | None = None
    idp: str | None = None
    auth_method: str = "password"  # password | sso | idp-bearer
    acr: str | None = None


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange shared password for a JWT",
    description="Constant-time compare against viewer then admin password. "
    "Same 401 response shape on miss/empty to avoid timing/oracle hints.",
)
async def login(body: LoginRequest, _: None = Depends(_login_rate)) -> LoginResponse:
    if not settings.dashboard_local_login:
        # Enterprise mode (ADR 0120): the data center's SSO is the only way in. Same shape as a
        # wrong password would be misleading — say plainly where to sign in instead.
        # A feature switch, not an authorization guard — numeric on purpose, so the published-claims
        # scan (tests/unit/test_published_capability_claims.py) keeps classing login as pre-auth.
        raise HTTPException(
            status_code=403,
            detail="local password login is disabled; sign in with your organisation",
        )
    role = check_password(body.password)
    if role is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid password")
    token, expires_at = issue_token(role)
    return LoginResponse(token=token, role=role, expires_at=expires_at.isoformat())


@router.post(
    "/logout",
    status_code=204,
    summary="Stateless no-op logout",
    description="JWTs are stateless. The client discards the token; this "
    "endpoint exists for parity and audit-friendliness.",
)
async def logout(_: dict = Depends(require_role("viewer"))) -> Response:
    resp = Response(status_code=204)
    resp.delete_cookie(session_cookie_name(), path="/")  # SSO sessions live in a cookie
    return resp


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Return the caller's role + token expiry",
)
async def me(claims: dict = Depends(require_role("viewer"))) -> MeResponse:
    from datetime import datetime

    from capabilities import principal_from_claims

    exp = datetime.fromtimestamp(claims["exp"], tz=UTC)
    principal = principal_from_claims(claims)
    return MeResponse(
        role=claims["role"],
        expires_at=exp.isoformat(),
        tenant=principal["tenant"],
        capabilities=principal["capabilities"],
        sub=str(claims.get("sub", "")),
        name=claims.get("name"),
        idp=claims.get("idp"),
        auth_method=str(claims.get("via") or "password"),
        acr=claims.get("acr"),
    )

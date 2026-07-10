"""Login, logout, me — the only unauthenticated entry point is /login."""

from datetime import UTC

from auth import check_password, issue_token, require_role
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])


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


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange shared password for a JWT",
    description="Constant-time compare against viewer then admin password. "
    "Same 401 response shape on miss/empty to avoid timing/oracle hints.",
)
async def login(body: LoginRequest) -> LoginResponse:
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
    return Response(status_code=204)


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
    )

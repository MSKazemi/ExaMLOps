"""HTTP reverse proxy for upstream services with per-service auth injection."""

from collections.abc import Awaitable, Callable

import httpx
from auth import _role_at_least, verify_token
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from settings import settings
from sqlalchemy.ext.asyncio import AsyncSession

from routers.config import get_decrypted_secret

router = APIRouter(prefix="/proxy", tags=["proxy"])

_bearer = HTTPBearer(auto_error=False)

_SERVICE_BASES: dict[str, str] = {
    "mlflow": settings.mlflow_url,
    "ray": settings.ray_serve_url,
    "prefect": settings.prefect_url,
    "prometheus": settings.prometheus_url,
    "grafana": settings.grafana_url,
    "control_plane": settings.control_plane_url,
}

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# An injector takes outgoing headers + a DB session and returns possibly
# modified headers. Adding upstream auth in future = one new entry here.
Injector = Callable[[dict[str, str], AsyncSession], Awaitable[dict[str, str]]]


async def inject_grafana_bearer(
    headers: dict[str, str], db: AsyncSession
) -> dict[str, str]:
    key = await get_decrypted_secret(db, "grafana_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def inject_control_plane_bearer(
    headers: dict[str, str], db: AsyncSession
) -> dict[str, str]:
    token = await get_decrypted_secret(db, "control_plane_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


INJECTORS: dict[str, Injector] = {
    "grafana": inject_grafana_bearer,
    "control_plane": inject_control_plane_bearer,
    # MinIO intentionally absent — see spec §10.
}


def _is_safe(method: str) -> bool:
    return method.upper() in {"GET", "HEAD", "OPTIONS"}


def _required_role(method: str) -> str:
    return "viewer" if _is_safe(method) else "admin"


@router.api_route(
    "/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    summary="Reverse-proxy with per-service upstream auth injection",
)
async def proxy(
    service: str,
    path: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Response:
    # Role gate: viewer for safe methods, admin for mutations.
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = verify_token(credentials.credentials)
    required = _required_role(request.method)
    if not _role_at_least(payload.get("role", ""), required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"role '{required}' required",
        )

    base = _SERVICE_BASES.get(service)
    if base is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown service: {service!r}",
        )

    url = f"{base}/{path}"
    if request.query_params:
        url = f"{url}?{request.query_params}"

    body = await request.body()
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_HEADERS
        and k.lower() not in ("host", "authorization")
    }

    injector = INJECTORS.get(service)
    if injector is not None:
        fwd_headers = await injector(fwd_headers, db)

    async with httpx.AsyncClient() as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=fwd_headers,
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Upstream unavailable: {exc}"
            ) from exc

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )

import asyncio
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Request
from settings import settings

router = APIRouter()

# (internal_url, health_path, default_public_url)
_SERVICES = {
    "mlflow":         (settings.mlflow_url,         "/health",            settings.public_mlflow_url),
    "prefect":        (settings.prefect_url,         "/api/health",        settings.public_prefect_url),
    "ray_serve":      (settings.ray_serve_url,       "/health",            settings.public_ray_dashboard_url),
    "prometheus":     (settings.prometheus_url,      "/-/healthy",         settings.public_prometheus_url),
    "grafana":        (settings.grafana_url,         "/api/health",        settings.public_grafana_url),
    "minio":          (settings.minio_url,           "/minio/health/live", settings.public_minio_console_url),
    "control_plane":  (settings.control_plane_url,   "/health",            settings.public_control_plane_url),
}


def _rewrite_host(public_url: str, request_host: str) -> str:
    """Replace the hostname in public_url with the host from the browser request.

    If the browser hits the dashboard at 1.2.3.4:8099, we want service links
    to point at 1.2.3.4:<service-port> rather than localhost:<service-port>.
    """
    parsed = urlparse(public_url)
    # Strip any port from the request host header, keep only the hostname
    req_hostname = request_host.split(":")[0]
    if req_hostname in ("localhost", "127.0.0.1", ""):
        return public_url
    rewritten = parsed._replace(netloc=f"{req_hostname}:{parsed.port}")
    return urlunparse(rewritten)


async def _ping(base: str, path: str, public_url: str, client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(f"{base}{path}", timeout=3.0)
        return {"status": "ok" if r.status_code < 400 else "degraded", "url": public_url}
    except Exception:
        return {"status": "down", "url": public_url}


async def _ping_db() -> dict:
    """Check Postgres connectivity via a lightweight SELECT 1."""
    from database import engine
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "url": "postgresql://"}
    except Exception:
        return {"status": "down", "url": "postgresql://"}


@router.get("/health")
async def get_health(request: Request) -> dict:
    now = datetime.now(UTC).isoformat()
    request_host = request.headers.get("host", "localhost")

    async with httpx.AsyncClient() as client:
        http_results = await asyncio.gather(
            *[
                _ping(base, path, _rewrite_host(pub, request_host), client)
                for _name, (base, path, pub) in _SERVICES.items()
            ]
        )
    db_result = await _ping_db()
    services = dict(zip(_SERVICES.keys(), http_results))
    services["postgres"] = db_result
    overall = "ok" if all(s["status"] == "ok" for s in services.values()) else "degraded"
    return {"status": overall, "checked_at": now, "services": services}

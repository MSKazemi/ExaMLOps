import asyncio
import time
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Request
from settings import settings

router = APIRouter()

_CACHE_TTL = 30.0   # seconds — lower probe frequency on NFS-backed shared cluster
_PROBE_TIMEOUT = 8.0  # seconds — NFS services can spike; 3s was too tight
_cache: dict = {}
_cache_lock = asyncio.Lock()
_probe_in_progress: set[str] = set()

# (internal_url, health_path, default_public_url)
_SERVICES = {
    "mlflow":         (settings.mlflow_url,                   "/health",            settings.public_mlflow_url),
    "prefect":        (settings.prefect_url,                   "/api/health",        settings.public_prefect_url),
    "ray_serve":      (settings.ray_serve_url,                 "/health",            settings.public_ray_dashboard_url),
    "prometheus":     (settings.prometheus_url,                "/-/healthy",         settings.public_prometheus_url),
    "grafana":        (settings.grafana_url,                   "/api/health",        settings.public_grafana_url),
    "minio":          (settings.minio_url,                     "/minio/health/live", settings.public_minio_console_url),
    "control_plane":  (settings.control_plane_url,             "/health",            settings.public_control_plane_url),
    "loki":           (settings.loki_url,                      "/ready",             settings.public_loki_url),
    "seanerbus":      (settings.seanerbus_bridge_status_url,   "/health",            settings.public_seanerbus_bridge_url),
    "jupyterhub":     (settings.jupyterhub_url,                "/hub/api/",          settings.public_jupyterhub_url),
}


def _rewrite_host(public_url: str, request_host: str) -> str:
    """Replace the hostname in public_url with the host from the browser request.

    If the browser hits the dashboard at 1.2.3.4:8099, we want service links
    to point at 1.2.3.4:<service-port> rather than localhost:<service-port>.
    """
    parsed = urlparse(public_url)
    req_hostname = request_host.split(":")[0]
    if req_hostname in ("localhost", "127.0.0.1", ""):
        return public_url
    rewritten = parsed._replace(netloc=f"{req_hostname}:{parsed.port}")
    return urlunparse(rewritten)


async def _ping(base: str, path: str, public_url: str, client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(f"{base}{path}", timeout=_PROBE_TIMEOUT)
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


async def _do_health_check(request_host: str) -> dict:
    now = datetime.now(UTC).isoformat()
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

    # Dashboard is self — always ok if we're responding.
    services["dashboard"] = {"status": "ok", "url": _rewrite_host(settings.public_dashboard_url, request_host)}

    # Slurm adapter is inline (no HTTP endpoint); report ok in mock mode, down in real mode.
    slurm_status = "ok" if settings.slurm_mode == "mock" else "down"
    services["slurm"] = {"status": slurm_status, "url": ""}

    # SeanerBUS Sim is an external Cap'n Proto bus — proxy its reachability from bridge health.
    services["seanerbus_sim"] = {"status": services["seanerbus"]["status"], "url": ""}

    overall = "ok" if all(s["status"] == "ok" for s in services.values()) else "degraded"
    return {"status": overall, "checked_at": now, "services": services}


@router.get("/health")
async def get_health(request: Request) -> dict:
    request_host = request.headers.get("host", "localhost")
    cache_key = request_host

    # Fast path: serve cache without acquiring the probe lock.
    entry = _cache.get(cache_key)
    if entry and (time.monotonic() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]

    # Slow path: only one probe per cache_key at a time.
    # Concurrent requests while a probe is in flight get stale data instead of
    # queueing — this prevents a slow NFS service from blocking all callers.
    async with _cache_lock:
        # Re-check: another coroutine may have refreshed while we waited for the lock.
        entry = _cache.get(cache_key)
        if entry and (time.monotonic() - entry["ts"]) < _CACHE_TTL:
            return entry["data"]

        if cache_key in _probe_in_progress:
            # Probe already running — return stale data rather than piling up.
            return entry["data"] if entry else {"status": "starting", "services": {}}

        _probe_in_progress.add(cache_key)

    # Run probe outside the lock so the lock isn't held for up to _PROBE_TIMEOUT seconds.
    try:
        data = await _do_health_check(request_host)
        async with _cache_lock:
            _cache[cache_key] = {"data": data, "ts": time.monotonic()}
        return data
    finally:
        async with _cache_lock:
            _probe_in_progress.discard(cache_key)

from contextlib import asynccontextmanager
from pathlib import Path

from database import engine, init_db
from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from routers import (
    ab_testing,
    admission,
    alerts,
    approvals,
    audit,
    auth,
    autopilot,
    batch,
    bff,
    cards,
    collab,
    compliance,
    config,
    connections,
    containers,
    copilot,
    docs,
    drift_data,
    events,
    explain,
    facility,
    fairness,
    feature_store,
    features,
    finops,
    flags,
    gateway,
    governance,
    health,
    hpo,
    llmops,
    mlops,
    models,
    modelzoo,
    namespace,
    nextgen,
    pipelines,
    platform_audit,
    platform_data,
    projects,
    prompts,
    providers,
    proxy,
    quality,
    rollback,
    scaffold,
    scaling,
    seanerbus,
    search,
    secrets,
    selfobs,
    shadow,
    slo,
    traffic,
    workbenches,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    try:
        from settings import settings
        from storage import ImageStorage

        storage = ImageStorage(
            endpoint_url=settings.minio_url,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.dashboard_minio_bucket,
        )
        await storage.ensure_bucket()
    except Exception as exc:
        import logging

        logging.getLogger("dashboard").warning("MinIO bucket setup failed (non-fatal): %s", exc)
    yield
    await engine.dispose()


# Disable the built-in /docs + /redoc: FastAPI's defaults pull Swagger UI / ReDoc JS+CSS from a
# public CDN (cdn.jsdelivr.net), which the strict CSP set by SecurityHeadersMiddleware
# (F16 / ADR 0053: `script-src 'self'`) blocks in the browser — leaving the docs page blank.
# We re-serve /docs + /redoc below from same-origin vendored assets so the API docs render under
# the strict CSP and work offline on the HPC/lxp deploy. `/openapi.json` stays on the default route.
app = FastAPI(
    title="ExaMLOps Dashboard",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# Vendored Swagger UI / ReDoc assets (same-origin ⇒ CSP `script-src 'self'` allows them).
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/docs", include_in_schema=False)
async def swagger_ui_html() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
        swagger_favicon_url="/static/favicon.png",
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect() -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html() -> HTMLResponse:
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - ReDoc",
        redoc_js_url="/static/redoc.standalone.js",
        redoc_favicon_url="/static/favicon.png",
        with_google_fonts=False,  # Google Fonts stylesheet would be CSP-blocked; ReDoc degrades fine.
    )


# Security-hardening baseline (F16 / ADR 0053): strict CSP + security headers on every response,
# with frame-ancestors scoped to the Grafana embed origin (F5).
from security import SecurityHeadersMiddleware  # noqa: E402

try:
    from settings import settings as _settings

    _grafana_origin = _settings.public_grafana_url
except Exception:  # pragma: no cover - settings optional in some test paths
    _grafana_origin = "http://localhost:13000"

app.add_middleware(SecurityHeadersMiddleware, grafana_origin=_grafana_origin)

# Self-observability (F24 / ADR 0067): record request/latency/status metrics for the status page.
from selfobs import MetricsMiddleware  # noqa: E402

app.add_middleware(MetricsMiddleware)

# Health is unauthenticated (load balancer / k8s probes)
app.include_router(health.router, prefix="/api")

# Backend-for-Frontend view layer (F8): the dashboard's `/api/v1/*` aggregation surface.
app.include_router(bff.router, prefix="/api")
app.include_router(mlops.router, prefix="/api")
app.include_router(facility.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(finops.router, prefix="/api")
app.include_router(selfobs.router, prefix="/api")
app.include_router(governance.router, prefix="/api")
app.include_router(llmops.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(flags.router, prefix="/api")
app.include_router(copilot.router, prefix="/api")
app.include_router(collab.router, prefix="/api")

# Auth: login is unauthenticated; me/logout require viewer.
app.include_router(auth.router, prefix="/api")

# Auth-protected API routes
app.include_router(config.router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(proxy.router, prefix="/api")
app.include_router(docs.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(modelzoo.router, prefix="/api")
app.include_router(seanerbus.router, prefix="/api")
app.include_router(containers.router, prefix="/api")
app.include_router(approvals.router, prefix="/api")
app.include_router(pipelines.router, prefix="/api")
app.include_router(scaffold.router, prefix="/api")
app.include_router(platform_audit.router, prefix="/api")
app.include_router(drift_data.router, prefix="/api")
app.include_router(compliance.router, prefix="/api")
app.include_router(gateway.router, prefix="/api")
app.include_router(prompts.router, prefix="/api")
app.include_router(autopilot.router, prefix="/api")
app.include_router(slo.router, prefix="/api")
app.include_router(scaling.router, prefix="/api")
app.include_router(admission.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(traffic.router, prefix="/api")
app.include_router(secrets.router, prefix="/api")
app.include_router(feature_store.router, prefix="/api")
app.include_router(fairness.router, prefix="/api")
app.include_router(platform_data.router, prefix="/api")
app.include_router(rollback.router, prefix="/api")
app.include_router(quality.router, prefix="/api")
app.include_router(shadow.router, prefix="/api")
app.include_router(ab_testing.router, prefix="/api")
app.include_router(batch.router, prefix="/api")
app.include_router(explain.router, prefix="/api")
app.include_router(hpo.router, prefix="/api")
app.include_router(cards.router, prefix="/api")
app.include_router(features.router, prefix="/api")
app.include_router(namespace.router, prefix="/api")
app.include_router(nextgen.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(providers.router, prefix="/api")
app.include_router(connections.router, prefix="/api")
app.include_router(workbenches.router, prefix="/api")

# Serve built React SPA — only when dist/ exists (skipped in test environment)
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(str(_dist / "index.html"))

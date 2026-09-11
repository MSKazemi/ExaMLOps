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
    assets,
    audit,
    auth,
    autopilot,
    batch,
    bff,
    cards,
    challenger,
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
    modules,
    namespace,
    nextgen,
    pipelines,
    platform_audit,
    platform_data,
    platform_ops,
    projects,
    prompts,
    providers,
    proxy,
    quality,
    rollback,
    scaffold,
    scaling,
    scim,
    seanerbus,
    search,
    secrets,
    selfobs,
    shadow,
    slo,
    sso,
    traffic,
    workbenches,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # A credential left at its `.env.example` value is refused by `check_password`, so the
    # role simply stops logging in. Say why at startup, or that reads as "the password
    # broke" rather than "the password was never set".
    try:
        import logging

        from auth import is_placeholder
        from settings import settings as _s

        for _name, _val in (
            ("DASHBOARD_VIEWER_PASSWORD", _s.dashboard_viewer_password),
            ("DASHBOARD_ADMIN_PASSWORD", _s.dashboard_admin_password),
        ):
            if is_placeholder(_val):
                logging.getLogger("dashboard").error(
                    "%s is still the placeholder from .env.example — that role cannot log in. "
                    "Set a real value: python3 -c 'import secrets; print(secrets.token_urlsafe(24))'",
                    _name,
                )
    except Exception:  # never let a diagnostic stop the app from booting
        pass

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

# FastAPI types `openapi_url` and `swagger_ui_oauth2_redirect_url` as optional because either can
# be switched off. This app switches off only `docs_url`/`redoc_url` — precisely so the vendored
# routes below can replace them — so both of these keep FastAPI's own defaults. Narrowing once,
# here, states that assumption in one place instead of at three call sites.
_OPENAPI_URL: str = app.openapi_url or "/openapi.json"
_OAUTH2_REDIRECT_URL: str = app.swagger_ui_oauth2_redirect_url or "/docs/oauth2-redirect"

# Vendored Swagger UI / ReDoc assets (same-origin ⇒ CSP `script-src 'self'` allows them).
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/docs", include_in_schema=False)
async def swagger_ui_html() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=_OPENAPI_URL,
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=_OAUTH2_REDIRECT_URL,
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
        swagger_favicon_url="/static/favicon.png",
    )


@app.get(_OAUTH2_REDIRECT_URL, include_in_schema=False)
async def swagger_ui_redirect() -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html() -> HTMLResponse:
    return get_redoc_html(
        openapi_url=_OPENAPI_URL,
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

# Site feature profile (ADR 0128): API routes of modules this site switched off answer 404
# `module_disabled` — enforced here, not just hidden in the UI.
from module_gate import ModuleGateMiddleware  # noqa: E402

app.add_middleware(ModuleGateMiddleware)

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
# Organisation SSO (ADR 0120): providers/login/callback are unauthenticated by nature.
app.include_router(sso.router, prefix="/api")
# SCIM 2.0 provisioning (ADR 0132): authenticated by each center's own SCIM bearer, not a session.
app.include_router(scim.router, prefix="/api")

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
# Asset DAG + freshness, read-only (ADR 0036 clause 5); rebuilding stays `exa assets materialize`.
app.include_router(assets.router, prefix="/api")
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
app.include_router(platform_ops.router, prefix="/api")
app.include_router(rollback.router, prefix="/api")
app.include_router(quality.router, prefix="/api")
app.include_router(challenger.router, prefix="/api")
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
# Site feature profile (ADR 0128): which modules this centre runs.
app.include_router(modules.router, prefix="/api")

# Serve built React SPA — only when dist/ exists (skipped in test environment)
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(str(_dist / "index.html"))

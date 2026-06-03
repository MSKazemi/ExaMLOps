from contextlib import asynccontextmanager
from pathlib import Path

from database import engine, init_db
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from routers import (
    approvals,
    audit,
    auth,
    config,
    containers,
    docs,
    health,
    models,
    modelzoo,
    pipelines,
    proxy,
    scaffold,
    seanerbus,
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


app = FastAPI(title="ExaMLOps Dashboard", version="1.0.0", lifespan=lifespan)

# Health is unauthenticated (load balancer / k8s probes)
app.include_router(health.router, prefix="/api")

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

# Serve built React SPA — only when dist/ exists (skipped in test environment)
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(str(_dist / "index.html"))

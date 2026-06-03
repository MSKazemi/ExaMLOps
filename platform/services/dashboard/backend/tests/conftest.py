import os

# Set env vars before any app imports so pydantic-settings reads them fresh
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["DASHBOARD_VIEWER_PASSWORD"] = "test-viewer-pw"
os.environ["DASHBOARD_ADMIN_PASSWORD"] = "test-admin-pw"
os.environ["DASHBOARD_JWT_SECRET"] = "test-jwt-secret-32-bytes-of-zeros!"
os.environ["DASHBOARD_JWT_TTL_HOURS"] = "12"
os.environ["DASHBOARD_SECRET_KEY"] = "TVk4sP_ws6A6sRz38Kw1jJZX0d3Jcq3V0z0b6n6kE-c="

import pytest_asyncio
from database import Base, get_db
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

VIEWER_PW = "test-viewer-pw"
ADMIN_PW = "test-admin-pw"


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_engine):
    # Defer main/httpx imports until the fixture runs, so test collection
    # doesn't fail in intermediate states where main.py imports modules that
    # haven't been rewritten yet (e.g. auth/router refactors mid-branch).
    from httpx import ASGITransport, AsyncClient
    from main import app

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()

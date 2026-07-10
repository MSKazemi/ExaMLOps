import os

# --- moto <-> aiobotocore compatibility shim ---------------------------------
# storage.py talks to MinIO via aioboto3/aiobotocore; the S3 tests mock the
# backend with moto's ``mock_aws``. moto natively mocks *sync* botocore: its
# stubber returns a plain ``AWSResponse`` whose ``.raw`` is a ``BytesIO`` and
# whose ``.content`` is bytes. aiobotocore's async endpoint instead needs an
# ``AioAWSResponse`` (awaitable ``.content``) whose ``.raw`` exposes an async
# ``read()`` and ``raw_headers`` — hence the long-standing
# ``'MockRawResponse' object has no attribute 'raw_headers'`` /
# ``object bytes can't be used in 'await' expression`` failures
# (getmoto/moto#6836, aio-libs/aiobotocore#755).
#
# The dashboard only ever uses the async client, so within this test session we
# can unconditionally adapt moto's stubber output to the async shape. This keeps
# the storage tests exercising real code against moto instead of pinning to an
# ancient, no-longer-available moto/aiobotocore pair or skipping the suite.
try:  # pragma: no cover - exercised indirectly by tests/test_storage.py
    from aiobotocore.awsrequest import AioAWSResponse
    from moto.core.botocore_stubber import BotocoreStubber

    class _AsyncRaw:
        """Async-readable adapter over moto's sync ``MockRawResponse``."""

        def __init__(self, raw, headers):
            self._raw = raw
            self._headers = headers

        async def read(self, *args):
            return self._raw.read(*args)

        @property
        def raw_headers(self):
            return [
                (str(k).encode("utf-8"), str(v).encode("utf-8")) for k, v in self._headers.items()
            ]

    _orig_stubber_call = BotocoreStubber.__call__

    def _patched_stubber_call(self, event_name, request, **kwargs):  # noqa: ANN001
        resp = _orig_stubber_call(self, event_name, request, **kwargs)
        if resp is None:
            return None
        return AioAWSResponse(
            resp.url,
            resp.status_code,
            resp.headers,
            _AsyncRaw(resp.raw, dict(resp.headers)),
        )

    BotocoreStubber.__call__ = _patched_stubber_call  # type: ignore[method-assign]
except Exception:  # noqa: BLE001 - if moto/aiobotocore internals move, tests surface it
    pass

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
    from routers.auth import LOGIN_LIMITER

    # Fresh brute-force-limiter state per test — the limiter is process-global,
    # so without this the many logins across the suite would trip the 429 gate.
    LOGIN_LIMITER.reset()

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()

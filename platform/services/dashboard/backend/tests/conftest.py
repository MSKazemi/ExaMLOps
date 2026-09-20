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

import pytest
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


@pytest.fixture(autouse=True)
def _private_platform_db(tmp_path, monkeypatch):
    """Give every dashboard test its own ``PLATFORM_DB`` file, as the platform's own suite does.

    Without it a test that used only ``client`` — and did not ask for a ``platform_db`` fixture —
    resolved the default path and read the repository's real ``platform.db``, the one a local stack
    writes to: its result depended on whatever that stack had recorded, and a write would have
    landed in it. A test's own fixture still wins (it sets the variable after this one).
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))


@pytest.fixture(autouse=True)
def _isolate_postgres_state():
    """Give every dashboard test an empty platform datastore when running on Postgres.

    The `platform_db` fixtures in this suite point `PLATFORM_DB` at a `tmp_path` file, which is
    perfect isolation on SQLite and completely meaningless on Postgres — there every test in the
    process shares one schema. Without this they would see each other's rows, and the suite's
    result would depend on the order pytest happened to pick.

    Shared with the platform's own suite via `examlops.storage.testing` rather than copied, since
    a divergence between the two would be invisible until one of them started lying. No-op on
    SQLite, so the default path is exactly as it was.
    """
    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() != "postgres":
        yield
        return
    from examlops.storage.testing import postgres_isolation

    yield from postgres_isolation()


def pytest_configure(config):  # noqa: ARG001 - pytest's hook signature
    """On Postgres, give this xdist worker its own schema before anything connects.

    The fixture above empties *the* schema, so workers sharing one truncate each other's rows
    mid-test. This suite runs serially today, where the call is a no-op — it is here so that
    running it with `-n auto` is simply faster rather than quietly flaky.
    """
    from examlops.storage.testing import scope_schema_to_this_worker

    scope_schema_to_this_worker()


# --- a dashboard test may not talk to a running backing service --------------------------
#
# `test_dataplane_bus_status_unreachable_when_bridge_down` asserted the bridge probe fails while doing
# nothing to make it fail: it passed here because nothing was on :8003, and would have failed on
# any machine running `make dataplane-bus-up` or on the lxp node, where the bridge is a bare-metal
# process. `test_response_carries_security_headers` — a claim about middleware — went the other
# way and opened real connections to nine services, then left the result in the health router's
# 30-second process-global cache.
#
# Both are the same defect: the outcome depends on what happens to be running on the developer's
# machine rather than on the code under test. The ports come from `settings`, so a service added
# there is covered without editing this list. `database_url` is deliberately excluded — the
# Postgres backend run connects to it for real, and legitimately.

_SERVICE_PORTS: dict[int, str] | None = None
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _service_ports() -> dict[int, str]:
    global _SERVICE_PORTS
    if _SERVICE_PORTS is None:
        from urllib.parse import urlparse

        from settings import settings

        found: dict[int, str] = {}
        for field, value in vars(settings).items():
            if not field.endswith("_url") or field == "database_url" or not isinstance(value, str):
                continue
            parsed = urlparse(value)
            if parsed.hostname in _LOCAL_HOSTS and parsed.port:
                found.setdefault(parsed.port, field)
        _SERVICE_PORTS = found
    return _SERVICE_PORTS


@pytest.fixture
def guarded_ports() -> dict[int, str]:
    """The port→setting map the live-service guard enforces, exposed for its own tests.

    `test_live_service_guard.py` cannot `import conftest`, and re-deriving the list there would
    let the test and the guard drift apart — which is the one failure this fixture exists to make
    impossible.
    """
    return dict(_service_ports())


class LiveServiceContacted(BaseException):
    """Raised when a test reaches a platform service port on this host.

    A ``BaseException``, not an ``Exception``, and that is the whole point: every service probe in
    this repo wraps its socket call in ``except Exception``, so an ``AssertionError`` raised here
    was caught by the code under test and became "the service is down" — guard silent, test green,
    machine still measured. ``BaseException`` passes through those handlers the way
    ``KeyboardInterrupt`` does. Each suite keeps its own copy on purpose (see this file's header).
    """


@pytest.fixture(autouse=True)
def _no_live_backing_services():
    # Restores by hand rather than through `monkeypatch`: an autouse conftest fixture that requests
    # `monkeypatch` pulls it earlier in setup order for *every* test, which inverts teardown order
    # against any fixture that quietly assumed monkeypatch had already undone its env. That is
    # exactly what `tests/test_settings.py::restore_settings_module` assumed, and it errored the
    # moment this fixture existed. A guard is not allowed to reorder the suite it guards.
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    ports = _service_ports()

    def _check(address):
        try:
            host, port = address[0], address[1]
        except (TypeError, IndexError, KeyError):
            return
        if port in ports and str(host) in _LOCAL_HOSTS:
            raise LiveServiceContacted(
                f"this test connected to {ports[port]} at {host}:{port}. Whether that service is "
                "running is a property of this machine, not of the code under test — mock the "
                "client (see tests/test_health.py) or point the setting at a port nothing serves."
            )

    def guard(self, address, *a, **k):
        _check(address)
        return real_connect(self, address, *a, **k)

    def guard_ex(self, address, *a, **k):
        _check(address)
        return real_connect_ex(self, address, *a, **k)

    socket.socket.connect = guard
    socket.socket.connect_ex = guard_ex
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex

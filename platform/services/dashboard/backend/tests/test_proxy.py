"""Proxy router: per-service auth injection + role gates."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, pw):
    r = await client.post("/api/auth/login", json={"password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeAsyncClient:
    """Records the headers + url passed to the upstream."""

    def __init__(self, status: int = 200):
        self.status = status
        self.captured: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, content, headers, timeout):
        self.captured["method"] = method
        self.captured["url"] = url
        self.captured["headers"] = dict(headers)

        class R:
            pass

        r = R()
        r.status_code = self.status
        r.content = b""
        r.headers = {}
        return r


def _has_auth_header(headers: dict, expected: str | None) -> bool:
    """Case-insensitive check for the Authorization header value."""
    for k, v in headers.items():
        if k.lower() == "authorization":
            if expected is None:
                return False
            return v == expected
    return expected is None


@pytest.mark.asyncio
async def test_proxy_requires_viewer_token(client):
    r = await client.get("/api/proxy/mlflow/health")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_proxy_post_requires_admin_role(client, monkeypatch):
    fake = _FakeAsyncClient()
    monkeypatch.setattr("routers.proxy.httpx.AsyncClient", lambda: fake)

    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/proxy/ray/reload", headers=_hdr(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_unknown_service_returns_404(client):
    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/proxy/unknown/x", headers=_hdr(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_grafana_proxy_injects_bearer_when_secret_set(client, db_engine, monkeypatch):
    from models import DashboardConfig
    from secret_store import encrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            DashboardConfig(
                key="grafana_api_key",
                value=None,
                secret_value=encrypt("g-key"),
                is_secret=True,
            )
        )
        await s.commit()

    fake = _FakeAsyncClient()
    monkeypatch.setattr("routers.proxy.httpx.AsyncClient", lambda: fake)

    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/proxy/grafana/api/dashboards", headers=_hdr(token))
    assert r.status_code == 200
    assert _has_auth_header(fake.captured["headers"], "Bearer g-key")


@pytest.mark.asyncio
async def test_grafana_proxy_no_bearer_when_secret_unset(client, db_engine, monkeypatch):
    fake = _FakeAsyncClient()
    monkeypatch.setattr("routers.proxy.httpx.AsyncClient", lambda: fake)

    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/proxy/grafana/api/dashboards", headers=_hdr(token))
    assert r.status_code == 200
    assert _has_auth_header(fake.captured["headers"], None)


@pytest.mark.asyncio
async def test_unknown_service_injector_forwards_unchanged(client, monkeypatch):
    fake = _FakeAsyncClient()
    monkeypatch.setattr("routers.proxy.httpx.AsyncClient", lambda: fake)

    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/proxy/mlflow/health", headers=_hdr(token))
    assert r.status_code == 200
    assert _has_auth_header(fake.captured["headers"], None)


@pytest.mark.asyncio
async def test_proxy_passes_upstream_5xx_through(client, monkeypatch):
    fake = _FakeAsyncClient(status=500)
    monkeypatch.setattr("routers.proxy.httpx.AsyncClient", lambda: fake)

    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/proxy/mlflow/bad", headers=_hdr(token))
    assert r.status_code == 500


@pytest.mark.asyncio
async def test_proxy_turns_an_upstream_auth_refusal_into_a_bad_gateway(client, monkeypatch):
    # An upstream 401/403 is about the dashboard's service credential, not the browser session;
    # forwarded as-is it logs the user out (the SPA treats 401/403 as session expiry).
    for upstream in (401, 403):
        fake = _FakeAsyncClient(status=upstream)
        monkeypatch.setattr("routers.proxy.httpx.AsyncClient", lambda fake=fake: fake)
        token = await _login(client, ADMIN_PW)
        r = await client.get("/api/proxy/mlflow/x", headers=_hdr(token))
        assert r.status_code == 502
        assert r.headers["x-upstream-status"] == str(upstream)

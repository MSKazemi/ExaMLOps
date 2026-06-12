"""Tests for ModelZoo CI pipeline trigger endpoints."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, password: str) -> str:
    r = await client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_gitlab_config(db_engine, pipeline_token: str | None = "trigger-tok"):
    from models import DashboardConfig
    from secret_store import encrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="gitlab_project_id", value="42", is_secret=False))
        s.add(
            DashboardConfig(key="gitlab_url", value="https://gitlab.example.com", is_secret=False)
        )
        if pipeline_token is not None:
            s.add(
                DashboardConfig(
                    key="gitlab_pipeline_token",
                    value=None,
                    secret_value=encrypt(pipeline_token),
                    is_secret=True,
                )
            )
        await s.commit()


@pytest.mark.asyncio
async def test_trigger_pipeline_requires_admin(client, db_engine):
    await _seed_gitlab_config(db_engine)
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/modelzoo/trigger-pipeline", headers=_hdr(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_trigger_pipeline_503_when_token_not_set(client, db_engine):
    await _seed_gitlab_config(db_engine, pipeline_token=None)
    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/modelzoo/trigger-pipeline", headers=_hdr(token))
    assert r.status_code == 503
    assert "GitLab pipeline token not configured" in r.json()["detail"]


@pytest.mark.asyncio
async def test_trigger_pipeline_calls_gitlab_and_returns_pipeline(client, db_engine, monkeypatch):
    await _seed_gitlab_config(db_engine)

    def _fake_client():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert "trigger/pipeline" in request.url.path
            return httpx.Response(
                201,
                json={
                    "id": 999,
                    "status": "pending",
                    "web_url": "https://gitlab.example.com/project/-/pipelines/999",
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("routers.modelzoo._http_client", _fake_client)

    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/modelzoo/trigger-pipeline", headers=_hdr(token))
    assert r.status_code == 200
    body = r.json()
    assert body["pipeline_id"] == 999
    assert body["status"] == "pending"
    assert "pipelines/999" in body["web_url"]


@pytest.mark.asyncio
async def test_pipeline_status_proxies_gitlab(client, db_engine, monkeypatch):
    await _seed_gitlab_config(db_engine)

    def _fake_client():
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/pipelines/999" in request.url.path
            return httpx.Response(
                200,
                json={
                    "id": 999,
                    "status": "passed",
                    "web_url": "https://gitlab.example.com/-/pipelines/999",
                    "duration": 87,
                    "created_at": "2026-05-15T10:00:00Z",
                    "finished_at": "2026-05-15T10:01:27Z",
                },
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("routers.modelzoo._http_client", _fake_client)

    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/modelzoo/pipeline-status/999", headers=_hdr(token))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "passed"
    assert body["duration_seconds"] == 87
    assert body["finished_at"] == "2026-05-15T10:01:27Z"

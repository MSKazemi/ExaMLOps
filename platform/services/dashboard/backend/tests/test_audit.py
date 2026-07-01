"""Audit router: admin-only paginated read."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_audit_requires_admin(client, db_engine):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/audit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_audit_returns_recent_rows(client, db_engine):
    from models import DashboardAudit

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        for k in ("grafana_api_key", "minio_access_key", "minio_secret_key"):
            s.add(DashboardAudit(role="admin", action="set", key=k))
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/audit?limit=2", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    # Most recent first.
    assert body["items"][0]["key"] == "minio_secret_key"


@pytest.mark.asyncio
async def test_audit_limit_capped_at_500(client):
    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/audit?limit=99999", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422

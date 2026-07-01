"""Config router: role gates, secret masking, PUT semantics, audit rows."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, password: str) -> str:
    r = await client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_get_config_requires_viewer_token(client):
    r = await client.get("/api/config")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_viewer_sees_url_keys_and_masked_secrets(client, db_engine):
    from models import DashboardConfig
    from secret_store import encrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="mlflow_url", value="http://m", is_secret=False))
        s.add(
            DashboardConfig(
                key="grafana_api_key",
                value=None,
                secret_value=encrypt("g-key"),
                is_secret=True,
            )
        )
        s.add(
            DashboardConfig(key="minio_access_key", value=None, secret_value=None, is_secret=True)
        )
        await s.commit()

    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/config", headers=_hdr(token))
    assert r.status_code == 200
    body = r.json()
    assert body["mlflow_url"] == "http://m"
    assert body["grafana_api_key"] == "***"
    assert body["minio_access_key"] is None


@pytest.mark.asyncio
async def test_get_config_keys_returns_metadata_only(client, db_engine):
    from models import DashboardConfig
    from secret_store import encrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="mlflow_url", value="http://m", is_secret=False))
        s.add(
            DashboardConfig(
                key="grafana_api_key",
                value=None,
                secret_value=encrypt("g"),
                is_secret=True,
            )
        )
        await s.commit()

    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/config/keys", headers=_hdr(token))
    assert r.status_code == 200
    rows = {row["key"]: row for row in r.json()}
    assert rows["mlflow_url"]["is_secret"] is False
    assert rows["mlflow_url"]["has_value"] is True
    assert rows["grafana_api_key"]["is_secret"] is True
    assert rows["grafana_api_key"]["has_value"] is True


@pytest.mark.asyncio
async def test_put_config_requires_admin_role(client):
    token = await _login(client, VIEWER_PW)
    r = await client.put("/api/config", json={"mlflow_url": "http://x"}, headers=_hdr(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_set_url(client, db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="mlflow_url", value="http://old", is_secret=False))
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.put("/api/config", json={"mlflow_url": "http://new"}, headers=_hdr(token))
    assert r.status_code == 200
    async with factory() as s:
        row = await s.get(DashboardConfig, "mlflow_url")
        assert row.value == "http://new"


@pytest.mark.asyncio
async def test_admin_can_set_secret_writes_audit_row(client, db_engine):
    from models import DashboardAudit, DashboardConfig
    from secret_store import decrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="grafana_api_key", value=None, secret_value=None, is_secret=True))
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.put("/api/config", json={"grafana_api_key": "topsecret"}, headers=_hdr(token))
    assert r.status_code == 200
    assert r.json()["grafana_api_key"] == "***"

    async with factory() as s:
        row = await s.get(DashboardConfig, "grafana_api_key")
        assert decrypt(row.secret_value) == "topsecret"
        audit = (await s.execute(select(DashboardAudit))).scalars().all()
        assert len(audit) == 1
        assert audit[0].action == "set"
        assert audit[0].key == "grafana_api_key"
        assert audit[0].role == "admin"


@pytest.mark.asyncio
async def test_admin_clear_secret_with_null(client, db_engine):
    from models import DashboardAudit, DashboardConfig
    from secret_store import encrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            DashboardConfig(
                key="grafana_api_key",
                value=None,
                secret_value=encrypt("g"),
                is_secret=True,
            )
        )
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.put("/api/config", json={"grafana_api_key": None}, headers=_hdr(token))
    assert r.status_code == 200
    async with factory() as s:
        row = await s.get(DashboardConfig, "grafana_api_key")
        assert row.secret_value is None
        audit = (await s.execute(select(DashboardAudit))).scalars().all()
        assert audit[-1].action == "clear"


@pytest.mark.asyncio
async def test_blank_string_for_secret_key_is_rejected(client, db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="grafana_api_key", value=None, secret_value=None, is_secret=True))
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.put("/api/config", json={"grafana_api_key": ""}, headers=_hdr(token))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_unknown_key_rejected_with_400(client):
    token = await _login(client, ADMIN_PW)
    r = await client.put("/api/config", json={"some_random_key": "x"}, headers=_hdr(token))
    assert r.status_code == 400

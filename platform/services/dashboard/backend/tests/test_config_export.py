"""Tests for POST /config/export-env."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, password: str) -> str:
    r = await client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_export_env_requires_admin(client):
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/config/export-env", headers=_hdr(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_export_env_empty_db_returns_400(client):
    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/config/export-env", headers=_hdr(token))
    assert r.status_code == 400
    assert "No config" in r.json()["detail"]


@pytest.mark.asyncio
async def test_export_env_maps_url_keys(client, db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="mlflow_url", value="http://mlflow:5000", is_secret=False))
        s.add(DashboardConfig(key="minio_url", value="http://minio:9000", is_secret=False))
        s.add(
            DashboardConfig(key="dataplane_bus_host", value="dataplane-bus-host", is_secret=False)
        )
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/config/export-env", headers=_hdr(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert 'attachment; filename=".env.dashboard"' in r.headers.get("content-disposition", "")
    content = r.text
    assert "MLFLOW_TRACKING_URI=http://mlflow:5000" in content
    assert "MLFLOW_S3_ENDPOINT_URL=http://minio:9000" in content
    assert "DATAPLANE_BUS_HOST=dataplane-bus-host" in content
    # Keys not in the mapping must not appear
    assert "mlflow_url" not in content


@pytest.mark.asyncio
async def test_export_env_decrypts_secrets(client, db_engine):
    from models import DashboardConfig
    from secret_store import encrypt

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            DashboardConfig(
                key="minio_access_key",
                value=None,
                secret_value=encrypt("myaccesskey"),
                is_secret=True,
            )
        )
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/config/export-env", headers=_hdr(token))
    assert r.status_code == 200
    assert "AWS_ACCESS_KEY_ID=myaccesskey" in r.text


@pytest.mark.asyncio
async def test_export_env_skips_unset_secrets(client, db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            DashboardConfig(key="minio_access_key", value=None, secret_value=None, is_secret=True)
        )
        s.add(DashboardConfig(key="mlflow_url", value="http://ml:5000", is_secret=False))
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/config/export-env", headers=_hdr(token))
    assert r.status_code == 200
    assert "AWS_ACCESS_KEY_ID" not in r.text
    assert "MLFLOW_TRACKING_URI=http://ml:5000" in r.text


@pytest.mark.asyncio
async def test_export_env_audits_key_names_and_restricts_file_perms(
    client, db_engine, tmp_path, monkeypatch
):
    """D3: the bulk export is audited (key NAMES only) and the server-side file is 0600."""
    import os
    import stat

    from models import DashboardAudit, DashboardConfig
    from secret_store import encrypt
    from settings import settings
    from sqlalchemy import select

    export_path = tmp_path / ".env.dashboard"
    monkeypatch.setattr(settings, "env_export_path", str(export_path))

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="mlflow_url", value="http://mlflow:5000", is_secret=False))
        s.add(
            DashboardConfig(
                key="minio_secret_key",
                value=None,
                secret_value=encrypt("super-secret-value"),
                is_secret=True,
            )
        )
        await s.commit()

    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/config/export-env", headers=_hdr(token))
    assert r.status_code == 200

    # Server-side copy holds decrypted secrets — must be owner-only.
    assert export_path.exists()
    assert stat.S_IMODE(os.stat(export_path).st_mode) == 0o600

    async with factory() as s:
        rows = (
            (
                await s.execute(
                    select(DashboardAudit).where(DashboardAudit.action == "config_exported")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # Key names, never values.
    assert "mlflow_url" in rows[0].key
    assert "minio_secret_key" in rows[0].key
    assert "super-secret-value" not in rows[0].key

"""Schema tests: new columns/tables exist; CHECK constraint behaviour."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.mark.asyncio
async def test_dashboard_config_has_secret_columns(db_engine):
    from models import DashboardConfig

    cols = {c.name for c in DashboardConfig.__table__.columns}
    assert cols == {"key", "value", "secret_value", "is_secret", "updated_at"}


@pytest.mark.asyncio
async def test_dashboard_audit_table_exists():
    from models import DashboardAudit

    cols = {c.name for c in DashboardAudit.__table__.columns}
    assert cols == {"id", "at", "role", "action", "key"}


@pytest.mark.asyncio
async def test_can_insert_url_row(db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="mlflow_url", value="http://x", is_secret=False))
        await s.commit()


@pytest.mark.asyncio
async def test_can_insert_unset_secret_row(db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardConfig(key="grafana_api_key", value=None, secret_value=None, is_secret=True))
        await s.commit()


@pytest.mark.asyncio
async def test_can_insert_set_secret_row(db_engine):
    from models import DashboardConfig

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            DashboardConfig(
                key="grafana_api_key",
                value=None,
                secret_value=b"gAAAAA...ciphertext...",
                is_secret=True,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_audit_row_insert(db_engine):
    from models import DashboardAudit

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(DashboardAudit(role="admin", action="set", key="grafana_api_key"))
        await s.commit()

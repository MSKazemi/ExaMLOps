"""init_db seeds the three secret keys; migration is idempotent."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_init_db_seeds_secret_keys():
    from database import Base, _seed_secret_keys

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await _seed_secret_keys(s)
        await s.commit()

    from models import DashboardConfig

    async with factory() as s:
        rows = (await s.execute(select(DashboardConfig))).scalars().all()
        keys = {r.key: r for r in rows}
        for k in ("minio_access_key", "minio_secret_key", "grafana_api_key"):
            assert k in keys, f"missing seeded key {k}"
            assert keys[k].is_secret is True
            assert keys[k].value is None
            assert keys[k].secret_value is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_secret_keys_is_idempotent():
    from database import Base, _seed_secret_keys

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await _seed_secret_keys(s)
        await s.commit()
    async with factory() as s:
        await _seed_secret_keys(s)  # second call, must not insert dupes
        await s.commit()

    from models import DashboardConfig

    async with factory() as s:
        rows = (await s.execute(select(DashboardConfig))).scalars().all()
        keys = [r.key for r in rows]
        assert keys.count("minio_access_key") == 1
        assert keys.count("minio_secret_key") == 1
        assert keys.count("grafana_api_key") == 1

    await engine.dispose()

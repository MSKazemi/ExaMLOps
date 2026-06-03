from collections.abc import AsyncGenerator

from settings import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

SEEDED_SECRET_KEYS: tuple[str, ...] = (
    "minio_access_key",
    "minio_secret_key",
    "grafana_api_key",
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def _seed_secret_keys(session: AsyncSession) -> None:
    """Insert the in-scope secret keys with NULL values, idempotently."""
    from models import DashboardConfig  # local import to avoid circular

    existing = set(
        (await session.execute(select(DashboardConfig.key))).scalars().all()
    )
    for k in SEEDED_SECRET_KEYS:
        if k in existing:
            continue
        session.add(
            DashboardConfig(
                key=k, value=None, secret_value=None, is_secret=True
            )
        )


async def init_db() -> None:
    """Create tables + seed secret-key rows. Safe to run on every boot."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as session:
        await _seed_secret_keys(session)
        await session.commit()


# Register ORM models with Base.metadata at import time.
# This must come after Base is defined; models imports Base from here,
# so the circular reference is safe — Base already exists in this module's
# namespace by the time Python processes the import below.
import models as _models  # noqa: E402, F401

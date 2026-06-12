"""Alembic environment config — handle async SQLAlchemy and settings pre-load."""

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Add backend directory to sys.path so imports work within alembic
sys.path.insert(0, str(Path(__file__).parent.parent))

# Pre-populate required JWT secrets so settings.py can load.
# Settings declares these as required Field(...) with no defaults.
os.environ.setdefault("dashboard_viewer_password", "viewer")
os.environ.setdefault("dashboard_admin_password", "admin")
os.environ.setdefault("dashboard_jwt_secret", "jwt-secret-key")
os.environ.setdefault("dashboard_secret_key", "secret-key")

import models  # noqa: F401, E402  — registers ORM classes on Base.metadata
from database import Base  # noqa: E402

config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Allow DATABASE_URL env var to override sqlalchemy.url from alembic.ini.
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="dashboard_alembic_version",
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Helper for online migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table="dashboard_alembic_version",
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.
    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

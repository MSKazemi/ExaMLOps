"""Dashboard replicas starting together must not race their migrations (live Postgres).

Every replica runs ``alembic upgrade head`` as it starts. On an empty Postgres, concurrent
upgrades raced to create ``dashboard_alembic_version`` and the losers died on
``duplicate key value violates unique constraint "pg_type_typname_nsp_index"`` — 3 of 4
concurrent upgrades, measured on 2026-09-10 — so every fresh Helm install crash-restarted a
dashboard pod. ``alembic/env.py`` now takes a transaction-scoped advisory lock first.

Opt-in like the repository's other live Postgres tests: set ``EXAMLOPS_POSTGRES_TEST_DSN``
(``postgresql://user:pass@host:port/db`` for a role that may CREATE DATABASE).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

DSN = os.getenv("EXAMLOPS_POSTGRES_TEST_DSN", "")
BACKEND = Path(__file__).resolve().parents[1]
REPLICAS = 6

pytestmark = pytest.mark.skipif(not DSN, reason="set EXAMLOPS_POSTGRES_TEST_DSN to run")


def _with_db(dsn: str, db: str, scheme: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((scheme, parts.netloc, f"/{db}", parts.query, ""))


@pytest.fixture
def fresh_database():
    psycopg = pytest.importorskip("psycopg")
    name = f"alembic_race_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error as exc:  # the role cannot create databases: nothing to test here
        admin.close()
        pytest.skip(f"cannot CREATE DATABASE with this DSN: {exc}")
    try:
        yield name
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_concurrent_upgrades_on_an_empty_database_all_succeed(fresh_database):
    env = dict(os.environ)
    env["DATABASE_URL"] = _with_db(DSN, fresh_database, "postgresql+asyncpg")
    # -P: without it the working directory's own `alembic/` (the migrations folder) shadows the
    # installed alembic package. The console script `alembic upgrade head` does the same.
    alembic = [
        sys.executable,
        "-P",
        "-c",
        "from alembic.config import main; main(argv=['upgrade', 'head'])",
    ]
    procs = [
        subprocess.Popen(
            alembic,
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(REPLICAS)
    ]
    outputs = [p.communicate(timeout=180)[0] for p in procs]
    failed = [out[-600:] for p, out in zip(procs, outputs, strict=True) if p.returncode != 0]
    assert not failed, f"{len(failed)}/{REPLICAS} concurrent upgrades failed:\n" + "\n---\n".join(
        failed
    )

    import psycopg

    with psycopg.connect(_with_db(DSN, fresh_database, "postgresql")) as conn:
        rows = conn.execute("SELECT count(*) FROM dashboard_alembic_version").fetchone()
    assert rows == (1,), f"expected one head revision, found {rows}"

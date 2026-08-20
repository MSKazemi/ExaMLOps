"""A handler that raises mid-query must not cost a pooled connection — proved against a real pool.

``tests/test_connections_are_scoped.py`` is a *static* guard: it reads the source and asserts every
``connect()`` is released in a ``finally``. That pins the shape but proves nothing about the
runtime, so this test does the thing that actually demonstrates the fix.

The leak was never the deliberate rejections — those handlers call ``conn.close()`` *before* they
``raise HTTPException``, so a 400 released its connection. It was every *other* way out: the
release was written as a plain statement on the happy path, so any exception raised between the
``connect()`` and that line skipped it. A datastore error, a table missing because a migration was
half-applied, a bug in the code doing the transform — all of them walked past the close.

Under SQLite that costs a file handle the GC reclaims. Under ``EXAMLOPS_DB_BACKEND=postgres`` the
connection is *pooled*: one that is never returned is gone for the life of the process, the tenth
exhausts ``max_size``, and every later ``getconn()`` waits the pool's full 30-second budget — so
the failure presents as a hang rather than as a leak.

So: shrink the pool to two, then make the same handler fail more times than that. With the release
in a ``finally`` every attempt fails fast. Without it, the third blocks until the pool times out.
Postgres-only — on SQLite there is no pool to exhaust and nothing to prove — so it runs under
``make test-postgres`` and skips elsewhere.
"""

import os
import time

import pytest

from tests.conftest import ADMIN_PW

pytestmark = pytest.mark.skipif(
    os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() != "postgres",
    reason="there is no connection pool to exhaust on SQLite",
)

# Two, so a leak shows up on the third attempt rather than the eleventh.
_POOL_MAX = 2
_ATTEMPTS = 6

# psycopg_pool's own wait is 30 s and is deliberately left alone in the product (a shorter one
# would start failing legitimately-busy pools). A pass is therefore fast and a regression is slow,
# so bound it: a single exhausted wait already blows this budget, and the assertion says why.
_BUDGET_S = 20.0


@pytest.fixture
def tiny_pool(monkeypatch):
    """Rebuild the process-wide pool with room for two connections, and put it back afterwards."""
    from examlops.storage import pg

    def reset() -> None:
        with pg._POOL_LOCK:
            for pool in pg._POOLS.values():
                try:
                    pool.close()
                except Exception:
                    pass
            pg._POOLS.clear()

    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", str(_POOL_MAX))
    reset()
    yield
    monkeypatch.delenv("EXAMLOPS_POSTGRES_POOL_MAX", raising=False)
    reset()


@pytest.fixture
def missing_table(tmp_path, monkeypatch):
    """Take one table away, so the handler's very first query raises after it has connected.

    This is not a contrived fault: it is what a half-applied migration looks like from inside a
    request, and it lands in the one window that matters — after ``connect()``, before the close.
    """
    from examlops import platform_db as pdb

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    pdb.init_db()
    import dbconn

    conn = dbconn.connect(tmp_path / "platform.db", row_factory=None)
    try:
        conn.execute("DROP TABLE IF EXISTS drift_snapshots CASCADE")
        conn.commit()
    finally:
        conn.close()
    yield
    pdb.init_db()


async def test_a_failing_handler_returns_its_connection(client, tiny_pool, missing_table):
    token = (await client.post("/api/auth/login", json={"password": ADMIN_PW})).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    started = time.monotonic()
    for _ in range(_ATTEMPTS):
        try:
            await client.post("/api/drift/baseline/JPCP", headers=auth)
        except Exception:
            pass  # how it fails is this test's business only insofar as it fails *fast*
    elapsed = time.monotonic() - started

    assert elapsed < _BUDGET_S, (
        f"{_ATTEMPTS} failing requests took {elapsed:.1f}s against a pool of {_POOL_MAX}. "
        "Waiting like that means a request blocked waiting for a connection that an earlier "
        "failure never returned — the close is back on the happy path."
    )

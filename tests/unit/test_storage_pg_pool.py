"""Connection pooling for the Postgres backend (enterprise-readiness item 0.1 follow-on).

Opening a connection is free on SQLite and is not on Postgres, and ``platform_db.get_db()`` opens
one *per call* — a single dashboard page render makes dozens. Measured locally: 28.1 ms per
open→query→close unpooled against 3.3 ms pooled, and the gap widens the moment the database is on
another host.

These are pure tests: no driver, no server. They protect the parts that decide whether the pool is
safe — that a connection is handed back exactly once, that pooling degrades to the unpooled path
instead of failing, and that the size knobs cannot be configured into nonsense. The live pooled
round-trip is in ``tests/integration/test_postgres_backend_live.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

from examlops.storage import pg  # noqa: E402


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _FakePool:
    def __init__(self) -> None:
        self.returned: list[Any] = []

    def putconn(self, conn: Any) -> None:
        self.returned.append(conn)


# ── handing the connection back ───────────────────────────────────────────────


def test_pooled_close_returns_the_connection_instead_of_dropping_it():
    conn, pool = _FakeConn(), _FakePool()
    pg.PgConnection(conn, pool=pool).close()
    assert pool.returned == [conn]
    assert not conn.closed  # the whole point: it stays open for the next caller


def test_pooled_close_is_idempotent():
    """Two callers must never end up holding the same connection.

    ``get_db()`` closes in a ``finally`` while some call sites also close explicitly. Returning the
    same connection twice would put it in the pool twice — a data race that only shows under load.
    """
    conn, pool = _FakeConn(), _FakePool()
    wrapper = pg.PgConnection(conn, pool=pool)
    wrapper.close()
    wrapper.close()
    wrapper.close()
    assert pool.returned == [conn]


def test_pooled_close_rolls_back_first():
    """A read leaves the connection in a transaction; the pool would undo it, loudly."""
    conn, pool = _FakeConn(), _FakePool()
    pg.PgConnection(conn, pool=pool).close()
    assert conn.rollbacks == 1


def test_unpooled_close_really_closes():
    conn = _FakeConn()
    pg.PgConnection(conn).close()
    assert conn.closed


# ── configuration ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_pooling_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL", value)
    assert pg._pooling_enabled() is False
    assert pg._get_pool("postgresql://x/y", None) is None  # falls back to a direct connection


def test_pooling_is_on_by_default(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_POSTGRES_POOL", raising=False)
    assert pg._pooling_enabled() is True


def test_pool_size_defaults(monkeypatch):
    for var in ("EXAMLOPS_POSTGRES_POOL_MIN", "EXAMLOPS_POSTGRES_POOL_MAX"):
        monkeypatch.delenv(var, raising=False)
    assert pg._pool_size() == (1, 10)


def test_pool_size_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MIN", "2")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "20")
    assert pg._pool_size() == (2, 20)


def test_pool_size_cannot_be_configured_into_nonsense(monkeypatch):
    """min > max would make the pool refuse to open; garbage would crash on startup."""
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MIN", "50")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "4")
    assert pg._pool_size() == (4, 4)

    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MIN", "not-a-number")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "")
    assert pg._pool_size() == (1, 10)

    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "0")
    assert pg._pool_size()[1] == 1  # a zero-sized pool can never serve anyone


def test_a_missing_pool_library_degrades_instead_of_failing(monkeypatch):
    """The database must stay reachable when an *optional* dependency is absent."""
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL", "1")
    monkeypatch.setattr(pg, "_POOL_UNAVAILABLE", True)
    assert pg._get_pool("postgresql://x/y", None) is None


def test_schema_names_are_never_interpolated_raw(monkeypatch):
    with pytest.raises(ValueError, match="invalid schema name"):
        pg._configure_schema("public; DROP SCHEMA public CASCADE")
    with pytest.raises(ValueError, match="invalid schema name"):
        pg._ensure_schema("postgresql://x/y", "a-b; --")

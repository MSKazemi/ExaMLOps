"""StorageBackend repository seam (enterprise-readiness item 0.1).

Verifies the seam that lets the platform datastore become Postgres without touching call sites:
protocol conformance, the SQLite backend's byte-identical behaviour, the dialect helpers both
engines expose, and backend selection. The Postgres path is a skeleton (not runtime-verified) — we
only assert it constructs valid dialect SQL and fails loudly without a DSN.
"""

from __future__ import annotations

import pytest

from examlops.storage import (
    PostgresBackend,
    SqliteBackend,
    StorageBackend,
    get_backend,
)


def test_backends_satisfy_protocol():
    assert isinstance(SqliteBackend(), StorageBackend)
    assert isinstance(PostgresBackend(), StorageBackend)


def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DB_BACKEND", raising=False)
    assert get_backend().dialect == "sqlite"


def test_env_selects_postgres(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    assert get_backend().dialect == "postgres"


def test_sqlite_backend_connects_and_hardens(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "s.db"))
    conn = SqliteBackend().connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"  # hardened like every other platform connection
    finally:
        conn.close()


def test_sqlite_upsert_sql():
    sql = SqliteBackend().upsert_sql("t", ["a", "b"], ["a"])
    assert sql == "INSERT OR REPLACE INTO t (a, b) VALUES (?, ?)"


def test_postgres_dialect_helpers():
    pg = PostgresBackend()
    assert pg.now_expr() == "NOW()"
    sql = pg.upsert_sql("t", ["a", "b"], ["a"])
    assert "ON CONFLICT (a)" in sql
    assert "b=EXCLUDED.b" in sql  # non-key column updated
    assert "%s" in sql  # postgres param style


def test_postgres_connect_without_dsn_fails_loudly(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    with pytest.raises(RuntimeError, match="EXAMLOPS_POSTGRES_DSN"):
        PostgresBackend().connect()

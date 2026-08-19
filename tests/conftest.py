"""Shared pytest fixtures."""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _postgres_backend() -> bool:
    return os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres"


@pytest.fixture(autouse=True)
def _isolate_postgres_state():
    """Give every test an empty platform database when running on Postgres.

    On SQLite each test points ``PLATFORM_DB`` at its own ``tmp_path`` file, so isolation is free.
    On Postgres the file path means nothing — every test shares one schema — so without this they
    would see each other's rows and the suite's result would depend on ordering.

    Truncating (rather than re-creating 127 tables per test) keeps the run affordable, and
    ``RESTART IDENTITY`` matters because tests assert on generated ids. No-op on SQLite, so the
    default path is exactly as it was.
    """
    if not _postgres_backend():
        yield
        return
    sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))
    from examlops import platform_db as pdb

    pdb.init_db()
    rebuild = _reset(pdb)
    # Outside the transaction in `_reset` on purpose: re-running the DDL takes locks that its
    # connection still holds until the `with` block commits, and the two deadlock.
    if rebuild:
        pdb.init_db(force=True)
        _reset(pdb)
    yield


def _reset(pdb) -> bool:
    """Empty the schema; return whether the DDL needs re-running before the test starts."""
    global _NONEMPTY_SQL
    with pdb.get_db() as conn:
        try:
            dirty = [r[0] for r in conn.execute(_nonempty_tables_sql(conn)).fetchall()]
        except Exception:  # noqa: BLE001 — a test dropped a table; rebuild rather than fail here
            _NONEMPTY_SQL = None
            return True
        if dirty:
            conn.execute(
                "TRUNCATE " + ", ".join(f'"{t}"' for t in dirty) + " RESTART IDENTITY CASCADE"
            )
        # Rows are not the only state a test can leave behind: one of them drops the audit
        # guard trigger to play the attacker. On SQLite the next test gets a fresh file; here the
        # schema persists, so restore it whenever something is missing (cheap check, rare rebuild).
        return _trigger_count(conn) < _expected_triggers(conn)


_EXPECTED_TRIGGERS: int | None = None


def _trigger_count(conn) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
    ).fetchone()
    return int(row["n"])


def _expected_triggers(conn) -> int:
    global _EXPECTED_TRIGGERS
    if _EXPECTED_TRIGGERS is None:
        _EXPECTED_TRIGGERS = _trigger_count(conn)
    return _EXPECTED_TRIGGERS


_NONEMPTY_SQL: str | None = None


def _nonempty_tables_sql(conn) -> str:
    """One statement that names only the tables holding rows.

    Truncating all 127 tables costs ~2s per test (each TRUNCATE writes a new file node) — 70
    minutes across the suite. A test typically dirties one or two tables, and an ``EXISTS`` probe
    on an empty table is free, so this turns the per-test cost into noise.
    """
    global _NONEMPTY_SQL
    if _NONEMPTY_SQL is None:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
        ]
        _NONEMPTY_SQL = " UNION ALL ".join(
            f"SELECT '{t}' AS t WHERE EXISTS (SELECT 1 FROM \"{t}\")" for t in tables
        )
    return _NONEMPTY_SQL

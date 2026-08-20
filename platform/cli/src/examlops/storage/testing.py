"""Test-support for the Postgres backend: give each test an empty platform datastore.

A library that offers a second storage engine has to offer the test isolation that goes with
it, because the isolation strategy is not portable between the two. On SQLite a test points
``PLATFORM_DB`` at its own ``tmp_path`` file and isolation is free. On Postgres that path means
nothing — every test in a process shares one schema — so without help the tests see each other's
rows and the suite's result depends on the order pytest happened to pick.

This lives in the package rather than in one suite's ``conftest.py`` because two suites need it:
the platform's own tests and the dashboard's, which is a separate app with its own connection
adapter. Anything else built on ``EXAMLOPS_DB_BACKEND=postgres`` needs the same thing.

Usage, in a ``conftest.py``::

    @pytest.fixture(autouse=True)
    def _isolate_postgres_state():
        yield from postgres_isolation()

It is a no-op unless ``EXAMLOPS_DB_BACKEND=postgres``, so the default SQLite path is untouched.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

__all__ = ["postgres_backend", "postgres_isolation", "reset_state"]


def postgres_backend() -> bool:
    """Whether this process is configured to use the Postgres datastore."""
    return os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres"


def postgres_isolation() -> Iterator[None]:
    """Generator body for an autouse fixture: empty the schema before each test."""
    if not postgres_backend():
        yield
        return
    from examlops import platform_db as pdb

    pdb.init_db()
    rebuild = reset_state(pdb)
    # Outside the transaction in `reset_state` on purpose: re-running the DDL takes locks that
    # its connection still holds until the `with` block commits, and the two deadlock.
    if rebuild:
        pdb.init_db(force=True)
        reset_state(pdb)
    yield


def reset_state(pdb: Any) -> bool:
    """Empty the schema; return whether the DDL needs re-running before the test starts."""
    global _NONEMPTY_SQL, _TABLE_COUNT
    with pdb.get_db() as conn:
        try:
            dirty = [r[0] for r in conn.execute(_nonempty_tables_sql(conn)).fetchall()]
        except Exception:  # noqa: BLE001 — a test dropped a table; rebuild rather than fail here
            _NONEMPTY_SQL = None
            _TABLE_COUNT = -1
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


def _trigger_count(conn: Any) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
    ).fetchone()
    return int(row["n"])


def _expected_triggers(conn: Any) -> int:
    global _EXPECTED_TRIGGERS
    if _EXPECTED_TRIGGERS is None:
        _EXPECTED_TRIGGERS = _trigger_count(conn)
    return _EXPECTED_TRIGGERS


_NONEMPTY_SQL: str | None = None
_TABLE_COUNT: int = -1


def _nonempty_tables_sql(conn: Any) -> str:
    """One statement that names only the tables holding rows.

    Truncating all 127 tables costs ~2s per test (each TRUNCATE writes a new file node) — 70
    minutes across the suite. A test typically dirties one or two tables, and an ``EXISTS`` probe
    on an empty table is free, so this turns the per-test cost into noise.

    The probe is rebuilt whenever the table count moves. Not every table comes from
    ``platform_db.init_db()`` — ``examlops.connections`` and ``examlops.workbenches`` own their
    DDL and create it on first use — so a table can appear mid-run. Caching the probe once left
    exactly those tables untruncated, and the suite's result then depended on the order pytest
    happened to pick.
    """
    global _NONEMPTY_SQL, _TABLE_COUNT
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    ]
    if _NONEMPTY_SQL is None or len(tables) != _TABLE_COUNT:
        _TABLE_COUNT = len(tables)
        _NONEMPTY_SQL = " UNION ALL ".join(
            f"SELECT '{t}' AS t WHERE EXISTS (SELECT 1 FROM \"{t}\")" for t in tables
        )
    return _NONEMPTY_SQL


def empty_datastore(tmp_path: Any, monkeypatch: Any) -> str:
    """Point the platform at a datastore that exists but holds no tables — on either engine.

    Several surfaces make the same promise: on finding its table absent, degrade honestly —
    return ``False``, or fail loudly with a 500 — rather than report an empty result as though
    it had read one. Posing that question on SQLite is trivial: an empty file.

    On Postgres there is no file to be empty. ``PLATFORM_DB`` is ignored, and the shared schema
    always carries the full ~100 tables, so the same test quietly stopped asking its question and
    failed on the answer it got instead. Here it asks again, against a sibling schema that is
    deliberately never bootstrapped — one extra schema for the whole run, so the connection pool
    is not fragmented per test.

    Returns the ``PLATFORM_DB`` path, already set, for callers that pass it explicitly.
    """
    db = tmp_path / "empty.db"
    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres":
        base = os.getenv("EXAMLOPS_POSTGRES_SCHEMA", "public")
        monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", f"{base}_empty")
    else:
        db.touch()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)

"""StorageBackend — the repository seam behind ExaMLOps's shared datastore (ADR 0074-lineage, item 0.1).

**Why this exists.** Today ~252 `platform_db` helpers call SQLite directly. One SQLite file is the
platform's data layer *and* event bus *and* security plane *and* integration hub — and SQLite allows
exactly one writer process-wide, so it is the dominant throughput ceiling and the largest single
blast-radius SPOF at fleet scale. The enterprise fix is to put a thin **StorageBackend** seam in
front of the helpers so the engine can become Postgres (multi-writer, HA, backup, per-tenant
isolation) without touching call sites.

**What this module is.** The seam itself — a dialect-neutral backend protocol, the SQLite
implementation (byte-for-byte the current behaviour, via `resilience.db`), the Postgres one, and
the dialect helpers (`now_expr`, `upsert_sql`, parameter style) that let a query be written once
and run on either engine. Both engines are runtime-verified: the whole unit suite runs on each, and
`tests/integration/test_postgres_backend_live.py` covers the schema, the audit hash chain, the
append-only triggers and pooled concurrent writers against a real server.

Select the backend with ``EXAMLOPS_DB_BACKEND=sqlite|postgres`` (default ``sqlite``).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from examlops.resilience import db as _rdb


@runtime_checkable
class StorageBackend(Protocol):
    """A dialect-neutral datastore engine behind the platform helpers.

    Implementations own connection hardening and the small set of SQL-dialect differences that the
    helpers cannot express portably (parameter style, upsert syntax, "now" expression).
    """

    dialect: str
    paramstyle: str  # "qmark" (?) for SQLite, "format" (%s) for psycopg

    def connect(self) -> Any:
        """Open a hardened connection (WAL/busy_timeout for SQLite; a pooled conn for Postgres)."""
        ...

    def now_expr(self) -> str:
        """The SQL expression for the current timestamp in this dialect."""
        ...

    def upsert_sql(self, table: str, columns: Sequence[str], conflict: Sequence[str]) -> str:
        """Build an idempotent INSERT-or-replace statement keyed on ``conflict`` columns."""
        ...


class SqliteBackend:
    """The default engine — the platform's current behaviour, unchanged.

    Delegates connection hardening to :mod:`examlops.resilience.db` so every existing guarantee
    (WAL, ``synchronous=NORMAL``, ``busy_timeout``, ``Row`` factory) is preserved exactly.
    """

    dialect = "sqlite"
    paramstyle = "qmark"

    def __init__(self, path: str | None = None) -> None:
        self._path: str = path or os.getenv("PLATFORM_DB") or "./platform.db"

    def connect(self) -> sqlite3.Connection:
        return _rdb.connect(self._path)

    def now_expr(self) -> str:
        return "CURRENT_TIMESTAMP"

    def upsert_sql(self, table: str, columns: Sequence[str], conflict: Sequence[str]) -> str:
        # SQLite: INSERT OR REPLACE is the established idiom used across platform_db today.
        cols = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        return f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"


class PostgresBackend:
    """Enterprise engine — multi-writer, HA-capable, per-tenant-isolatable.

    :meth:`connect` returns a :class:`examlops.storage.pg.PgConnection`: a SQLite-shaped connection
    that translates the platform's SQLite dialect on the way through (see that module for the
    translation table). That is what lets ``EXAMLOPS_DB_BACKEND=postgres`` move the datastore
    without editing the ~252 helpers, all of which go through ``platform_db.get_db()``.

    Requires the driver (``pip install 'examlops[postgres]'``) and ``EXAMLOPS_POSTGRES_DSN``.
    Connections come from a per-``(dsn, schema)`` pool; see :func:`examlops.storage.pg.connect`.
    """

    dialect = "postgres"
    paramstyle = "format"

    def __init__(self, dsn: str | None = None, schema: str | None = None) -> None:
        self._dsn = dsn or os.getenv("EXAMLOPS_POSTGRES_DSN", "")
        # One Postgres database can hold several independent platform instances, one per schema.
        # That is what gives the test suite isolation without a database per test, and what a
        # multi-tenant deployment uses to keep tenants apart on one server.
        self._schema = schema or os.getenv("EXAMLOPS_POSTGRES_SCHEMA", "") or None

    def connect(self) -> Any:
        if not self._dsn:
            raise RuntimeError(
                "PostgresBackend requires EXAMLOPS_POSTGRES_DSN (e.g. "
                "postgresql://user:pw@host:5432/examlops)."
            )
        try:
            from examlops.storage import pg  # noqa: PLC0415 - optional enterprise dependency
        except ImportError as exc:  # pragma: no cover - defensive
            raise RuntimeError("examlops.storage.pg is unavailable") from exc
        try:
            return pg.connect(self._dsn, schema=self._schema)
        except ImportError as exc:
            raise RuntimeError(
                "PostgresBackend needs the 'psycopg' driver: pip install 'examlops[postgres]'."
            ) from exc

    def now_expr(self) -> str:
        return "NOW()"

    def upsert_sql(self, table: str, columns: Sequence[str], conflict: Sequence[str]) -> str:
        # Postgres: INSERT ... ON CONFLICT (keys) DO UPDATE SET non-key = EXCLUDED.non-key
        cols = ", ".join(columns)
        placeholders = ", ".join("%s" for _ in columns)
        conflict_cols = ", ".join(conflict)
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in set(conflict))
        stmt = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT ({conflict_cols})"
        return f"{stmt} DO UPDATE SET {updates}" if updates else f"{stmt} DO NOTHING"


def get_backend() -> StorageBackend:
    """Return the configured backend (``EXAMLOPS_DB_BACKEND``; default ``sqlite``)."""
    choice = os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower()
    if choice == "postgres":
        return PostgresBackend()
    return SqliteBackend()

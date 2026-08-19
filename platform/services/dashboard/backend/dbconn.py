"""The dashboard's connection to platform state.

Two engines, one entry point. Every `platform.db` access in the backend goes through
`connect()`, which is what makes this file the only place that has to know which engine is
configured:

* **SQLite (default)** — a hardened connection mirroring `examlops.resilience.db`: WAL +
  `synchronous=NORMAL` + a `busy_timeout`, so concurrent reads/writes wait out lock
  contention instead of raising "database is locked" 500s.
* **Postgres** (`EXAMLOPS_DB_BACKEND=postgres`) — the same `sqlite3`-shaped connection the
  CLI uses, from `examlops.storage.pg`. The `path` argument is meaningless there and is
  ignored; without this the dashboard would open an empty SQLite file while the CLI wrote
  to Postgres, and show empty consoles with no error anywhere.

The Postgres path needs the core package importable (the container puts
`platform/cli/src` on `PYTHONPATH`). If it is not, the dashboard keeps working on SQLite
rather than failing to start — but it says so, once, because that combination means the
consoles are reading the wrong store.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5000
_warned = False


def _postgres_configured() -> bool:
    return os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres"


def _connect_postgres() -> Any | None:
    """A Postgres connection, or None if the core package is not importable here."""
    global _warned
    try:
        from examlops.storage import get_backend

        return get_backend().connect()
    except Exception:  # noqa: BLE001 — never take the dashboard down over this
        if not _warned:
            _warned = True
            logger.error(
                "EXAMLOPS_DB_BACKEND=postgres but examlops.storage is unavailable — "
                "falling back to SQLite, so this dashboard is NOT reading platform state. "
                "Put platform/cli/src on PYTHONPATH and install examlops[postgres]."
            )
        return None


def connect(path: str, *, row_factory=sqlite3.Row) -> Any:
    if _postgres_configured():
        conn = _connect_postgres()
        if conn is not None:
            return conn
    conn = sqlite3.connect(path, check_same_thread=False)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn

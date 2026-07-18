"""Hardened SQLite connection helper for the dashboard (mirrors examlops.resilience.db).

The dashboard is a separate app and can't import the core package, so it carries its own
copy of the WAL + synchronous=NORMAL + busy_timeout hardening. Every platform.db access in
the backend must go through connect() so concurrent reads/writes wait out lock contention
instead of raising 'database is locked' 500s.
"""

from __future__ import annotations

import sqlite3

_BUSY_TIMEOUT_MS = 5000


def connect(path: str, *, row_factory=sqlite3.Row) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn

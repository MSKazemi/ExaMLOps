"""SQLite connection hardening shared across every platform datastore.

The platform has three long-lived processes (CLI, agent service, dataplane bridge)
plus the control plane all opening SQLite files. Without a busy-timeout, concurrent
writers hit ``sqlite3.OperationalError: database is locked`` immediately. This module
centralizes the pragmas (WAL + NORMAL + busy_timeout) and a retry-on-locked wrapper
so every store gets the same protection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .retry import is_locked_error, retry_call
from .timeouts import DB_BUSY_TIMEOUT_MS


def harden(
    conn: sqlite3.Connection,
    *,
    wal: bool = True,
    busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Apply the standard resilience pragmas to an open connection."""
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms}"
    )
    return conn


def connect(
    path: str,
    *,
    wal: bool = True,
    check_same_thread: bool = False,
    busy_timeout_ms: int | None = None,
    row_factory: Callable | None = sqlite3.Row,
) -> sqlite3.Connection:
    """Open a hardened SQLite connection.

    ``check_same_thread=False`` is the default because platform datastores are shared
    by threaded services; callers are responsible for their own write serialization
    where needed (the busy_timeout handles cross-process contention).
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    if row_factory is not None:
        conn.row_factory = row_factory
    return harden(conn, wal=wal, busy_timeout_ms=busy_timeout_ms)


def write_retry[T](fn: Callable[[], T], *, retries: int = 4, base_delay: float = 0.05) -> T:
    """Run a write ``fn`` retrying on ``database is locked`` contention.

    Complements the busy_timeout: the timeout waits for a lock within one attempt;
    this retries the whole transaction if it still loses the race.
    """
    return retry_call(
        fn,
        retries=retries,
        base_delay=base_delay,
        retry_on=is_locked_error,
        label="db-write",
    )

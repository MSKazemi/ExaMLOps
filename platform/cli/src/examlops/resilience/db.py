"""SQLite connection hardening shared across every platform datastore.

The platform has three long-lived processes (CLI, agent service, dataplane bridge)
plus the control plane all opening SQLite files. Without a busy-timeout, concurrent
writers hit ``sqlite3.OperationalError: database is locked`` immediately. This module
centralizes the pragmas (WAL + NORMAL + busy_timeout) and a retry-on-locked wrapper
so every store gets the same protection.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable

from .retry import is_locked_error, retry_call
from .timeouts import DB_BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)

# Process-level count of writes that exhausted every retry and were re-raised (item 0.4). This is
# the "metric" half of "emit a metric/log on retry exhaustion instead of silently dropping": the
# WARNING below is the durable signal (scraped by Loki); this counter is the in-process gauge that
# health checks / tests can read. Guarded by a lock so concurrent writer threads count correctly.
_EXHAUSTION_LOCK = threading.Lock()
_EXHAUSTION_COUNT = 0


def write_retry_exhaustions() -> int:
    """Number of DB writes that exhausted all retries this process (item 0.4 observability)."""
    return _EXHAUSTION_COUNT


def _on_write_exhausted(exc: BaseException, attempts: int) -> None:
    global _EXHAUSTION_COUNT
    with _EXHAUSTION_LOCK:
        _EXHAUSTION_COUNT += 1
        total = _EXHAUSTION_COUNT
    logger.warning(
        "db-write exhausted %d attempts and is being re-raised (NOT silently dropped): %s "
        "[process exhaustion count=%d]",
        attempts,
        exc,
        total,
    )


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
    this retries the whole transaction if it still loses the race. If every retry is
    exhausted the failure is re-raised loudly — logged at WARNING and counted in
    :func:`write_retry_exhaustions` — so a lost write can never vanish silently (item 0.4).
    """
    return retry_call(
        fn,
        retries=retries,
        base_delay=base_delay,
        retry_on=is_locked_error,
        label="db-write",
        on_exhausted=_on_write_exhausted,
    )

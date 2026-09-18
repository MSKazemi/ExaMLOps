"""SQLite connection hardening shared across every platform datastore.

The platform has three long-lived processes (CLI, agent service, seanerbus bridge)
plus the control plane all opening SQLite files. Without a busy-timeout, concurrent
writers hit ``sqlite3.OperationalError: database is locked`` immediately. This module
centralizes the pragmas (WAL + NORMAL + busy_timeout) and a retry-on-locked wrapper
so every store gets the same protection.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import urllib.parse
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
    timeout_ms = DB_BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    if wal:
        _enable_wal(conn, timeout_ms)
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _enable_wal(conn: sqlite3.Connection, timeout_ms: int) -> None:
    """Switch to WAL, waiting out a concurrent opener instead of failing at once.

    Changing the journal mode needs an exclusive lock, and SQLite answers "database is locked"
    for it without calling the busy handler. Processes opening a new database together therefore
    failed immediately, all but one. The mode is stored in the file, so once any opener has
    switched it the retry here succeeds straight away.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    delay = 0.01
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


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


def connect_snapshot(path: str, *, row_factory: Callable | None = sqlite3.Row):
    """Open a file that must not be modified — a backup snapshot, an archived datastore.

    The hardened :func:`connect` above **writes**: `journal_mode=WAL` changes the file. That is
    right for a live datastore and wrong for anything being inspected, in two ways. It requires
    permission to modify what you are only reading — which the everyday arrangement does not give,
    since the Compose `backup` sidecar runs as root and its bundles are read back by an operator
    who is not root — and, more basically, a reader has no business altering the artefact.

    `immutable=1` alongside `mode=ro` states what is already true of a snapshot and stops SQLite
    reaching for the `-shm` side file that a WAL-mode header would otherwise make it want. Use this
    for any file the platform did not open in order to change.
    """
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


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

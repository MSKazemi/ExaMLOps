"""examlops.data.coordination — Coordination primitives (item 1.2).

Distributed locks, idempotency dedup, and fixed-window rate limits. This module now **owns** these
helpers (their bodies live here, not in ``platform_db``) — the per-domain split (item 4.5) with the
implementation physically relocated. Shared low-level primitives (``get_db``/``_immediate_write``/
``write_retry``) still come from the data layer; ``platform_db`` re-exports these for back-compat.
"""

from __future__ import annotations

from examlops.platform_db import _immediate_write, get_db, write_retry

__all__ = [
    "coord_try_lock",
    "coord_unlock",
    "coord_check_and_set_idempotent",
    "coord_rate_allow",
]


def coord_try_lock(key: str, holder: str, ttl_s: float) -> bool:
    """Acquire a named distributed lock (item 1.2). One conditional upsert = atomic.

    Returns True iff no live lock exists (or the caller already holds it). Expired locks are
    reclaimable; a crashed holder frees the lock by TTL.
    """
    ttl = max(1, int(ttl_s))

    def _lock() -> bool:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO coord_locks (key, holder, expires_at)
                   VALUES (?, ?, datetime(CURRENT_TIMESTAMP, ?))
                   ON CONFLICT(key) DO UPDATE SET
                       holder = excluded.holder, expires_at = excluded.expires_at
                   WHERE coord_locks.expires_at <= CURRENT_TIMESTAMP
                      OR coord_locks.holder = excluded.holder""",
                (key, holder, f"+{ttl} seconds"),
            )
            row = conn.execute(
                "SELECT holder FROM coord_locks WHERE key=? AND expires_at > CURRENT_TIMESTAMP",
                (key,),
            ).fetchone()
            return bool(row and row["holder"] == holder)

    return write_retry(_lock)


def coord_unlock(key: str, holder: str) -> None:
    """Release a lock iff ``holder`` owns it."""

    def _unlock() -> None:
        with get_db() as conn:
            conn.execute("DELETE FROM coord_locks WHERE key=? AND holder=?", (key, holder))

    write_retry(_unlock)


def coord_check_and_set_idempotent(key: str, ttl_s: float) -> bool:
    """Idempotency guard (item 1.2). Returns True the FIRST time ``key`` is seen, else False.

    Use to dedup retried/duplicate triggers (e.g. a webhook delivered twice): only the first caller
    gets True and should do the work. Entries expire after ``ttl_s`` so keys can be reused later.
    """
    ttl = max(1, int(ttl_s))

    def _cas() -> bool:
        with _immediate_write("coordination") as conn:
            # Purge expired first so a stale key doesn't wrongly suppress a fresh op.
            conn.execute("DELETE FROM coord_idempotency WHERE expires_at <= CURRENT_TIMESTAMP")
            cur = conn.execute(
                "INSERT OR IGNORE INTO coord_idempotency (key, expires_at) "
                "VALUES (?, datetime(CURRENT_TIMESTAMP, ?))",
                (key, f"+{ttl} seconds"),
            )
            return cur.rowcount == 1  # 1 = newly inserted (first time), 0 = duplicate

    return write_retry(_cas)


def coord_rate_allow(bucket: str, limit: int, window_s: float) -> bool:
    """Fixed-window rate limit (item 1.2). True if under ``limit`` for the current window.

    Atomically increments the window counter and returns whether the operation is allowed. When a
    new window starts the counter resets. Cross-process via the shared DB.
    """
    win = max(1, int(window_s))

    def _allow() -> bool:
        with _immediate_write("coordination") as conn:
            # Integer-second datetime arithmetic (not julianday floats) so the window boundary is
            # deterministic despite SQLite's whole-second CURRENT_TIMESTAMP resolution.
            row = conn.execute(
                "SELECT count, (window_start <= datetime(CURRENT_TIMESTAMP, ?)) AS elapsed "
                "FROM coord_rate WHERE bucket=?",
                (f"-{win} seconds", bucket),
            ).fetchone()
            fresh = row is None or bool(row["elapsed"])
            if fresh:
                conn.execute(
                    "INSERT INTO coord_rate (bucket, window_start, count) "
                    "VALUES (?, CURRENT_TIMESTAMP, 1) "
                    "ON CONFLICT(bucket) DO UPDATE SET window_start=CURRENT_TIMESTAMP, count=1",
                    (bucket,),
                )
                return True
            if row["count"] >= limit:
                return False
            conn.execute("UPDATE coord_rate SET count = count + 1 WHERE bucket=?", (bucket,))
            return True

    return write_retry(_allow)

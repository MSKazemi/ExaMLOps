"""examlops.data.idempotency — storage for result-carrying idempotency keys (ADR 0147 d4).

Policy lives in :mod:`examlops.idempotency`; this module only touches ``idempotency_keys``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = ["claim", "finish", "init_db", "release"]


def claim(key: str, scope: str, digest: str, pending_ttl_s: float) -> dict[str, Any] | None:
    """Claim ``key``. ``None`` = claimed (caller runs); else the existing row as a dict."""

    def _do() -> dict[str, Any] | None:
        now = time.time()
        with _immediate_write("idempotency") as conn:
            conn.execute("DELETE FROM idempotency_keys WHERE expires_at <= ?", (now,))
            cur = conn.execute(
                "INSERT INTO idempotency_keys "
                "(key, scope, request_hash, state, created_at, expires_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?) ON CONFLICT (key) DO NOTHING",
                (key, scope, digest, now, now + pending_ttl_s),
            )
            if cur.rowcount == 1:
                return None
            row = conn.execute(
                "SELECT request_hash, state, result_json FROM idempotency_keys WHERE key=?",
                (key,),
            ).fetchone()
            return dict(row) if row else {}

    return write_retry(_do)


def finish(key: str, result: dict[str, Any], ttl_s: float) -> None:
    def _do() -> None:
        now = time.time()
        with get_db() as conn:
            conn.execute(
                "UPDATE idempotency_keys SET state='done', result_json=?, expires_at=? WHERE key=?",
                (json.dumps(result, default=str), now + ttl_s, key),
            )

    write_retry(_do)


def release(key: str) -> None:
    def _do() -> None:
        with get_db() as conn:
            conn.execute("DELETE FROM idempotency_keys WHERE key=? AND state='pending'", (key,))

    write_retry(_do)

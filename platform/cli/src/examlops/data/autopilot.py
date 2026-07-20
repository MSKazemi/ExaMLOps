"""examlops.data.autopilot — Autopilot (ADR 0085).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry, write_retry  # noqa: F401

__all__ = [
    "claim_autopilot_lease",
    "create_autopilot_run",
    "get_autopilot_config",
    "list_autopilot_runs",
    "release_autopilot_lease",
    "set_autopilot_config",
    "update_autopilot_run",
]


def claim_autopilot_lease(holder: str, ttl_s: float) -> bool:
    """Atomically claim the single autopilot cycle lease for ``holder`` (Phase 0 item 0.12).

    Returns ``True`` iff no other holder currently owns a live lease — the claim + stamp are one
    ``INSERT … ON CONFLICT DO UPDATE … WHERE expires_at <= now`` statement, so two concurrent
    cron cycles / replicas cannot both acquire. The lease carries a TTL, so a crashed holder's
    lease auto-expires (no manual cleanup, no deadlock). Re-acquiring while already holding it
    (same ``holder``) succeeds and extends the TTL.
    """
    ttl = max(1, int(ttl_s))

    def _claim() -> bool:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO autopilot_lease (id, holder, acquired_at, expires_at)
                   VALUES (1, ?, CURRENT_TIMESTAMP, datetime(CURRENT_TIMESTAMP, ?))
                   ON CONFLICT(id) DO UPDATE SET
                       holder = excluded.holder,
                       acquired_at = excluded.acquired_at,
                       expires_at = excluded.expires_at
                   WHERE autopilot_lease.expires_at <= CURRENT_TIMESTAMP""",
                (holder, f"+{ttl} seconds"),
            )
            row = conn.execute(
                "SELECT holder FROM autopilot_lease WHERE id = 1 AND expires_at > CURRENT_TIMESTAMP"
            ).fetchone()
            return bool(row and row["holder"] == holder)

    return write_retry(_claim)


def create_autopilot_run(
    triggered_by: str = "manual",
    model_filter: str | None = None,
    dry_run: bool = False,
    enabled_state: str = "enabled",
) -> int:
    """Insert a new autopilot_runs row and return its id."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO autopilot_runs
               (triggered_by, model_filter, dry_run, enabled_state)
               VALUES (?,?,?,?)""",
            (triggered_by, model_filter, int(dry_run), enabled_state),
        )
        return cur.lastrowid


def get_autopilot_config(key: str) -> str | None:
    """Return a value from autopilot_config, or None if not set."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT value FROM autopilot_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def list_autopilot_runs(last_n: int = 10) -> list[dict[str, Any]]:
    """Return the last N autopilot run records, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM autopilot_runs ORDER BY id DESC LIMIT ?", (last_n,)
        ).fetchall()
    return [dict(r) for r in rows]


def release_autopilot_lease(holder: str) -> None:
    """Release the autopilot lease iff ``holder`` owns it (never steals another holder's lease)."""

    def _release() -> None:
        with get_db() as conn:
            conn.execute("DELETE FROM autopilot_lease WHERE id = 1 AND holder = ?", (holder,))

    write_retry(_release)


def set_autopilot_config(key: str, value: str) -> None:
    """Upsert a key/value pair in autopilot_config."""
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO autopilot_config (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )


def update_autopilot_run(
    run_id: int,
    *,
    retrains_triggered: int = 0,
    promotions_made: int = 0,
    policy_blocks: int = 0,
    human_required: int = 0,
    skipped: int = 0,
    summary: dict[str, Any] | None = None,
) -> None:
    """Update counts and summary for a completed autopilot run."""
    with get_db() as conn:
        conn.execute(
            """UPDATE autopilot_runs
               SET retrains_triggered=?, promotions_made=?, policy_blocks=?,
                   human_required=?, skipped=?, summary=?
               WHERE id=?""",
            (
                retrains_triggered,
                promotions_made,
                policy_blocks,
                human_required,
                skipped,
                json.dumps(summary) if summary else None,
                run_id,
            ),
        )


install_write_retry(__name__)

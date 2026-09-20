"""examlops.data.plans — storage for agent plans (ADR 0147 decision 2).

Policy lives in :mod:`examlops.plans`; this module only touches ``agent_plans``. The one
concurrency-sensitive operation is :func:`claim_apply`: a single conditional UPDATE, so of any
number of simultaneous applies of one plan exactly one sees ``rowcount == 1``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = [
    "claim_apply",
    "finish",
    "get",
    "init_db",
    "list_plans",
    "put",
    "set_approval",
    "set_state",
]

_COLS = (
    "plan_hash, tool, plan_json, state, actor, created_at, expires_at, approval_hash, "
    "approved_by, applied_at, applied_by, result_json"
)


def put(plan_hash: str, tool: str, plan: dict[str, Any], actor: str, expires_at: float) -> str:
    """Store a plan. Returns ``"created"``, ``"existing"`` (an identical live plan) or ``"reset"``.

    The hash is content-addressed, so re-planning an identical world returns the live plan
    untouched; a plan that already ended (applied/failed/expired/rejected) is reset to
    ``planned`` with a fresh expiry, which is what a genuinely new plan of the same change is.
    """

    def _do() -> str:
        now = time.time()
        with _immediate_write("agent_plans") as conn:
            row = conn.execute(
                "SELECT state, expires_at FROM agent_plans WHERE plan_hash=?", (plan_hash,)
            ).fetchone()
            if row is None:
                conn.execute(
                    f"INSERT INTO agent_plans ({_COLS}) "
                    "VALUES (?, ?, ?, 'planned', ?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
                    (plan_hash, tool, json.dumps(plan, default=str), actor, now, expires_at),
                )
                return "created"
            live = row["state"] in ("planned", "applying") and row["expires_at"] > now
            if live:
                return "existing"
            conn.execute(
                "UPDATE agent_plans SET plan_json=?, state='planned', actor=?, created_at=?, "
                "expires_at=?, approval_hash=NULL, approved_by=NULL, applied_at=NULL, "
                "applied_by=NULL, result_json=NULL WHERE plan_hash=?",
                (json.dumps(plan, default=str), actor, now, expires_at, plan_hash),
            )
            return "reset"

    return write_retry(_do)


def get(plan_hash: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM agent_plans WHERE plan_hash=?", (plan_hash,)
            ).fetchone()
            return dict(row) if row else None

    return write_retry(_do)


def list_plans(state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        with get_db() as conn:
            if state:
                rows = conn.execute(
                    f"SELECT {_COLS} FROM agent_plans WHERE state=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_COLS} FROM agent_plans ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    return write_retry(_do)


def claim_apply(plan_hash: str, actor: str) -> bool:
    """Atomically move ``planned`` -> ``applying``. True for exactly one caller.

    Refuses an expired plan in the same statement, so a plan cannot be claimed in the instant
    between an expiry check and the claim.
    """

    def _do() -> bool:
        now = time.time()
        with _immediate_write("agent_plans") as conn:
            cur = conn.execute(
                "UPDATE agent_plans SET state='applying', applied_by=?, applied_at=? "
                "WHERE plan_hash=? AND state='planned' AND expires_at > ?",
                (actor, now, plan_hash, now),
            )
            return cur.rowcount == 1

    return write_retry(_do)


def set_state(plan_hash: str, state: str, *, only_from: tuple[str, ...] | None = None) -> bool:
    def _do() -> bool:
        with _immediate_write("agent_plans") as conn:
            if only_from:
                marks = ",".join("?" for _ in only_from)
                cur = conn.execute(
                    f"UPDATE agent_plans SET state=? WHERE plan_hash=? AND state IN ({marks})",
                    (state, plan_hash, *only_from),
                )
            else:
                cur = conn.execute(
                    "UPDATE agent_plans SET state=? WHERE plan_hash=?", (state, plan_hash)
                )
            return cur.rowcount == 1

    return write_retry(_do)


def finish(plan_hash: str, state: str, result: dict[str, Any]) -> None:
    def _do() -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_plans SET state=?, result_json=? WHERE plan_hash=?",
                (state, json.dumps(result, default=str), plan_hash),
            )

    write_retry(_do)


def set_approval(plan_hash: str, approval_hash: str, approved_by: str) -> bool:
    """Record a human approval on a live ``planned`` plan (replaces any earlier one)."""

    def _do() -> bool:
        now = time.time()
        with _immediate_write("agent_plans") as conn:
            cur = conn.execute(
                "UPDATE agent_plans SET approval_hash=?, approved_by=? "
                "WHERE plan_hash=? AND state='planned' AND expires_at > ?",
                (approval_hash, approved_by, plan_hash, now),
            )
            return cur.rowcount == 1

    return write_retry(_do)

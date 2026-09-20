"""examlops.data.reasoning_budgets — storage for gateway reasoning budgets (ADR 0035 clause 2).

Policy (resolution, enforcement) lives in :mod:`examlops.structured`; this module only touches
``reasoning_budgets`` (the configured caps) and ``reasoning_budget_events`` (what the gateway
observed against them: ``within`` / ``exceeded`` / ``unknown`` / ``refused``).
"""

from __future__ import annotations

import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = ["applicable", "list_budgets", "list_events", "put", "record_event", "remove"]

SCOPES = ("key", "project", "model")


def put(scope: str, ref: str, max_thinking_tokens: int, tenant: str = "default") -> str:
    """Set the cap for one ``(scope, ref, tenant)``. Returns ``"created"`` or ``"updated"``."""

    def _do() -> str:
        init_db()
        now = time.time()
        with _immediate_write("reasoning_budgets") as conn:
            cur = conn.execute(
                "UPDATE reasoning_budgets SET max_thinking_tokens=?, updated_at=? "
                "WHERE scope=? AND ref=? AND tenant=?",
                (max_thinking_tokens, now, scope, ref, tenant),
            )
            if cur.rowcount == 1:
                return "updated"
            conn.execute(
                "INSERT INTO reasoning_budgets (scope, ref, tenant, max_thinking_tokens, "
                "updated_at) VALUES (?,?,?,?,?)",
                (scope, ref, tenant, max_thinking_tokens, now),
            )
            return "created"

    return write_retry(_do)


def remove(scope: str, ref: str, tenant: str = "default") -> bool:
    def _do() -> bool:
        init_db()
        with _immediate_write("reasoning_budgets") as conn:
            cur = conn.execute(
                "DELETE FROM reasoning_budgets WHERE scope=? AND ref=? AND tenant=?",
                (scope, ref, tenant),
            )
            return cur.rowcount > 0

    return write_retry(_do)


def list_budgets(tenant: str | None = None) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT scope, ref, tenant, max_thinking_tokens, updated_at FROM reasoning_budgets"
        args: list[Any] = []
        if tenant:
            sql += " WHERE tenant=?"
            args.append(tenant)
        with get_db() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY scope, ref, tenant", args)]

    return write_retry(_do)


def applicable(
    tenant: str, *, key_hash: str | None, project: str | None, model: str
) -> list[dict[str, Any]]:
    """Every configured cap that applies to this request (key, project, model)."""
    wanted = [("model", model)]
    if key_hash:
        wanted.append(("key", key_hash))
    if project:
        wanted.append(("project", project))

    def _do() -> list[dict[str, Any]]:
        init_db()
        out: list[dict[str, Any]] = []
        with get_db() as conn:
            for scope, ref in wanted:
                row = conn.execute(
                    "SELECT scope, ref, max_thinking_tokens FROM reasoning_budgets "
                    "WHERE scope=? AND ref=? AND tenant=?",
                    (scope, ref, tenant),
                ).fetchone()
                if row:
                    out.append(dict(row))
        return out

    return write_retry(_do)


def record_event(
    *,
    model: str,
    tenant: str,
    outcome: str,
    budget_tokens: int | None,
    observed_tokens: int | None,
    source: str | None,
    key_hash: str | None = None,
    project: str | None = None,
    request_id: str | None = None,
) -> None:
    def _do() -> None:
        init_db()
        with _immediate_write("reasoning_budget_events") as conn:
            conn.execute(
                "INSERT INTO reasoning_budget_events (ts, model, tenant, key_hash, project, "
                "outcome, budget_tokens, observed_tokens, source, request_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    model,
                    tenant,
                    key_hash,
                    project,
                    outcome,
                    budget_tokens,
                    observed_tokens,
                    source,
                    request_id,
                ),
            )

    write_retry(_do)


def list_events(
    outcome: str | None = None, model: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        where, args = [], []
        if outcome:
            where.append("outcome=?")
            args.append(outcome)
        if model:
            where.append("model=?")
            args.append(model)
        sql = "SELECT * FROM reasoning_budget_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with get_db() as conn:
            return [
                dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit])
            ]

    return write_retry(_do)

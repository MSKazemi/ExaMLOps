"""examlops.data.tool_grants - storage for the agent tool broker (ADR 0145).

Policy (the grant schema, ``decide_tool_call``, the broker) lives in :mod:`examlops.tool_broker`;
this module only touches ``tool_grants`` and ``tool_call_counters``.

Rate-limit counters are **bounded**: every ``consume`` prunes minute windows older than two
minutes and session windows untouched for a day, so the table cannot grow without limit. All
statements are portable SQL (``?`` placeholders, no upsert dialect) so the SQLite and Postgres
backends behave the same.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = [
    "consume",
    "counter",
    "delete_grant",
    "get_grants",
    "init_db",
    "list_grants",
    "list_subjects",
    "put_grant",
]

_MINUTE_KEEP = 2
_SESSION_TTL_S = 86400.0


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["grant"] = json.loads(d.pop("grant_json"))
    return d


def put_grant(subject: str, tool: str, grant: dict[str, Any], *, actor: str | None) -> bool:
    """Insert or replace one grant; returns ``True`` when it replaced an existing one."""

    def _do() -> bool:
        init_db()
        blob = json.dumps(grant, sort_keys=True, default=str)
        now = time.time()
        with _immediate_write("tool_grants") as conn:
            existed = (
                conn.execute(
                    "SELECT 1 FROM tool_grants WHERE subject=? AND tool=?", (subject, tool)
                ).fetchone()
                is not None
            )
            if existed:
                conn.execute(
                    "UPDATE tool_grants SET grant_json=?, actor=?, updated_at=? "
                    "WHERE subject=? AND tool=?",
                    (blob, actor, now, subject, tool),
                )
            else:
                conn.execute(
                    "INSERT INTO tool_grants (subject, tool, grant_json, actor, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (subject, tool, blob, actor, now),
                )
            return existed

    return write_retry(_do)


def delete_grant(subject: str, tool: str | None = None) -> int:
    """Remove one grant, or every grant of ``subject`` when ``tool`` is ``None``; returns count."""

    def _do() -> int:
        init_db()
        with _immediate_write("tool_grants") as conn:
            sql = "DELETE FROM tool_grants WHERE subject=?"
            args: list[Any] = [subject]
            if tool is not None:
                sql += " AND tool=?"
                args.append(tool)
            cur = conn.execute(sql, args)
            return int(cur.rowcount or 0)

    return write_retry(_do)


def get_grants(subject: str) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_grants WHERE subject=? ORDER BY tool", (subject,)
            ).fetchall()
        return [_row(r) for r in rows]

    return write_retry(_do)


def list_grants(subject: str | None = None) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM tool_grants"
        args: list[Any] = []
        if subject:
            sql += " WHERE subject=?"
            args.append(subject)
        sql += " ORDER BY subject, tool"
        with get_db() as conn:
            return [_row(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def list_subjects() -> list[str]:
    def _do() -> list[str]:
        init_db()
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT subject FROM tool_grants ORDER BY subject")
            return [r["subject"] for r in rows.fetchall()]

    return write_retry(_do)


def consume(
    subject: str, tool: str, limits: list[tuple[str, int, int]], *, now: float | None = None
) -> str | None:
    """Count one call against every ``(scope, window, max)`` limit, atomically.

    Returns the ``scope`` of the first limit that is already exhausted (and counts **nothing**),
    or ``None`` after incrementing all of them. Minute scopes are ``"minute"`` with the window
    ``int(now // 60)``; a session scope is ``"session:<id>"`` with window ``0``.
    """
    t = time.time() if now is None else now

    def _do() -> str | None:
        init_db()
        with _immediate_write("tool_call_counters") as conn:
            conn.execute(
                "DELETE FROM tool_call_counters WHERE scope=? AND window<?",
                ("minute", int(t // 60) - _MINUTE_KEEP),
            )
            conn.execute(
                "DELETE FROM tool_call_counters WHERE scope<>? AND updated_at<?",
                ("minute", t - _SESSION_TTL_S),
            )
            seen: dict[tuple[str, int], int | None] = {}
            for scope, window, cap in limits:
                r = conn.execute(
                    "SELECT n FROM tool_call_counters WHERE subject=? AND tool=? "
                    "AND scope=? AND window=?",
                    (subject, tool, scope, window),
                ).fetchone()
                have = int(r["n"]) if r else None
                if (have or 0) >= cap:
                    return scope
                seen[(scope, window)] = have
            for (scope, window), have in seen.items():
                if have is None:
                    conn.execute(
                        "INSERT INTO tool_call_counters (subject, tool, scope, window, n, "
                        "updated_at) VALUES (?,?,?,?,?,?)",
                        (subject, tool, scope, window, 1, t),
                    )
                else:
                    conn.execute(
                        "UPDATE tool_call_counters SET n=?, updated_at=? WHERE subject=? "
                        "AND tool=? AND scope=? AND window=?",
                        (have + 1, t, subject, tool, scope, window),
                    )
            return None

    return write_retry(_do)


def counter(subject: str, tool: str, scope: str, window: int) -> int:
    def _do() -> int:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                "SELECT n FROM tool_call_counters WHERE subject=? AND tool=? AND scope=? "
                "AND window=?",
                (subject, tool, scope, window),
            ).fetchone()
        return int(r["n"]) if r else 0

    return write_retry(_do)

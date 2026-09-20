"""Agent runs (AgentOps, ADR 0021) - read-only replay of Skipper sessions and tool analytics.

Reads (viewer, tenant-scoped like every other console): the recent ``agent_sessions`` index, one
session's replay (summary plus ordered tool calls), the per-tool success table, and the circuit-
breaker warnings/trips. Mirrors ``exa agentops sessions|replay|tools|anomalies``.

Read-only by construction: nothing here starts, stops or edits an agent. Tool arguments are never
returned - only the redacted digest the recorder stored (D8), exactly what ``exa agentops replay``
shows. A failed read is a ``503`` naming the surface, never an empty all-clear.
"""

from __future__ import annotations

import json

from auth import require_role
from capabilities import scope_to_tenant, tenant_visible
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Depends, HTTPException, Query, status
from readfail import readable

router = APIRouter(prefix="/agentops", tags=["agentops"])
_viewer = require_role("viewer")

_SESSION_COLS = (
    "session_id, tenant, agent, model, steps, tool_calls, errors, input_tokens, output_tokens, "
    "cost_usd, status, anomalies, started_at, ended_at"
)


def _db_path() -> str:
    return platform_db_path()


def _session(row) -> dict:
    d = dict(row)
    raw = d.get("anomalies")
    try:
        d["anomalies"] = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        d["anomalies"] = []
    return d


@router.get("/sessions")
async def list_sessions(
    principal: dict = Depends(_viewer),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Recent agent sessions, newest first, scoped to the caller's tenant."""
    with readable("the agent-session register"):
        conn = connect(_db_path())
        try:
            where, params = "", []
            if status_filter:
                where, params = "WHERE status=?", [status_filter]
            # Fetch a bounded multiple, then scope: a tenant filter applied after LIMIT would hand
            # a busy neighbour's rows the whole page and leave this caller's list short.
            rows = conn.execute(
                f"SELECT {_SESSION_COLS} FROM agent_sessions {where} "
                "ORDER BY started_at DESC LIMIT ?",
                (*params, limit * 5),
            ).fetchall()
        finally:
            conn.close()
    return scope_to_tenant(principal, [_session(r) for r in rows])[:limit]


@router.get("/sessions/{session_id}")
async def session_replay(session_id: str, principal: dict = Depends(_viewer)) -> dict:
    """One session: the summary plus its ordered tool calls (redacted digests, never raw args)."""
    with readable("the agent-session register"):
        conn = connect(_db_path())
        try:
            head = conn.execute(
                f"SELECT {_SESSION_COLS} FROM agent_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            calls = (
                conn.execute(
                    "SELECT step, tool, args_digest, ok, error, latency_ms, ts "
                    "FROM agent_tool_calls WHERE session_id=? ORDER BY step, id",
                    (session_id,),
                ).fetchall()
                if head
                else []
            )
        finally:
            conn.close()
    # Not-visible and not-found answer the same, so an id cannot be probed across tenants.
    if head is None or not tenant_visible(principal, dict(head).get("tenant")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent session")
    return {"session": _session(head), "steps": [dict(c) for c in calls]}


@router.get("/tools")
async def tool_success(principal: dict = Depends(_viewer)) -> list[dict]:
    """Per-tool call count, success rate and mean latency (tenant-scoped)."""
    with readable("the agent tool-call register"):
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT tenant, tool, COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok, "
                "AVG(latency_ms) AS avg_latency_ms FROM agent_tool_calls GROUP BY tenant, tool"
            ).fetchall()
        finally:
            conn.close()
    merged: dict[str, dict] = {}
    for r in scope_to_tenant(principal, [dict(r) for r in rows]):
        m = merged.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "ok": 0, "lat": []})
        m["calls"] += int(r["calls"])
        m["ok"] += int(r["ok"])
        if r["avg_latency_ms"] is not None:
            m["lat"].append((float(r["avg_latency_ms"]), int(r["calls"])))
    out = []
    for m in merged.values():
        weight = sum(n for _, n in m["lat"])
        out.append(
            {
                "tool": m["tool"],
                "calls": m["calls"],
                "errors": m["calls"] - m["ok"],
                "success_rate": m["ok"] / m["calls"] if m["calls"] else 0.0,
                "avg_latency_ms": sum(v * n for v, n in m["lat"]) / weight if weight else None,
            }
        )
    return sorted(out, key=lambda t: -t["calls"])


@router.get("/breaker")
async def breaker_events(_=Depends(_viewer), limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """Recent circuit-breaker warnings and aborts (from the audit log, newest first)."""
    with readable("the agent circuit-breaker log"):
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT ts, action, target, details FROM audit_events WHERE source='agentops' "
                "AND action IN ('agent_breaker_warning','agent_breaker_tripped') "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d["details"]) if d["details"] else {}
        except (ValueError, TypeError):
            d["details"] = {}
        # The session id is not returned: breaker rows carry no tenant, so it could name another
        # tenant's session. The replay route above is where a session is opened, scoped.
        d["details"].pop("session_id", None)
        d["event"] = "tripped" if d["action"].endswith("tripped") else "warning"
        out.append(d)
    return out

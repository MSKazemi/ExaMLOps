"""examlops.data.agent — Agent sessions + tool telemetry.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "get_agent_session_trace",
    "agent_metrics_rollup",
    "list_agent_sessions",
    "record_agent_session",
    "record_agent_tool_call",
    "tool_success_rate",
]


def get_agent_session_trace(session_id: str) -> dict[str, Any]:
    """Full session replay: summary + ordered tool-call steps (R6)."""
    init_db()
    with get_db() as conn:
        head = conn.execute(
            "SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        steps = conn.execute(
            """SELECT step, tool, args_digest, ok, error, latency_ms, ts
               FROM agent_tool_calls WHERE session_id=? ORDER BY step, id""",
            (session_id,),
        ).fetchall()
    return {
        "session": dict(head) if head else None,
        "steps": [dict(s) for s in steps],
    }


def list_agent_sessions(
    *, tenant: str | None = None, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Recent agent sessions newest-first (R6 replay index)."""
    init_db()
    clauses, params = [], []
    if tenant:
        clauses.append("tenant=?")
        params.append(tenant)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT * FROM agent_sessions {where}
                ORDER BY started_at DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def record_agent_session(
    session_id: str,
    *,
    tenant: str = "default",
    agent: str | None = None,
    model: str | None = None,
    steps: int = 0,
    tool_calls: int = 0,
    errors: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    status: str = "ok",
    anomalies: list[str] | None = None,
    ended: bool = False,
    started_at: str | None = None,
) -> None:
    """Upsert a session summary row (idempotent by ``session_id``).

    ``started_at`` (``YYYY-MM-DD HH:MM:SS``, UTC) is the wall-clock time the session really began.
    Sessions are flushed when they *end*, so without it the row's ``started_at`` defaults to the
    flush time and every duration would read as zero. It is written on insert only — an upsert
    never moves a session's start.
    """
    init_db()
    anom_json = json.dumps(anomalies) if anomalies else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agent_sessions
                   (session_id, tenant, agent, model, steps, tool_calls, errors,
                    input_tokens, output_tokens, cost_usd, status, anomalies,
                    started_at, ended_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, CURRENT_TIMESTAMP),
                       CASE WHEN ? THEN CURRENT_TIMESTAMP END)
               ON CONFLICT(session_id) DO UPDATE SET
                    tenant=excluded.tenant, agent=excluded.agent, model=excluded.model,
                    steps=excluded.steps, tool_calls=excluded.tool_calls,
                    errors=excluded.errors, input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens, cost_usd=excluded.cost_usd,
                    status=excluded.status, anomalies=excluded.anomalies,
                    ended_at=COALESCE(excluded.ended_at, agent_sessions.ended_at)""",
            (
                session_id,
                tenant,
                agent,
                model,
                steps,
                tool_calls,
                errors,
                input_tokens,
                output_tokens,
                cost_usd,
                status,
                anom_json,
                started_at,
                ended,
            ),
        )


def record_agent_tool_call(
    session_id: str,
    tool: str,
    *,
    tenant: str = "default",
    step: int = 0,
    args_digest: str | None = None,
    ok: bool = True,
    error: str | None = None,
    latency_ms: float | None = None,
) -> None:
    """Append one tool-call event (already-redacted ``args_digest`` from D8)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agent_tool_calls
                   (session_id, tenant, step, tool, args_digest, ok, error, latency_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            (session_id, tenant, step, tool, args_digest, 1 if ok else 0, error, latency_ms),
        )


def tool_success_rate(
    tool: str | None = None, *, tenant: str | None = None
) -> list[dict[str, Any]]:
    """Per-tool success rate + call count (R2) — for the AgentOps dashboard panel."""
    init_db()
    clauses, params = [], []
    if tool:
        clauses.append("tool=?")
        params.append(tool)
    if tenant:
        clauses.append("tenant=?")
        params.append(tenant)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT tool,
                       COUNT(*)                AS calls,
                       COALESCE(SUM(ok),0)     AS ok,
                       AVG(latency_ms)         AS avg_latency_ms
                FROM agent_tool_calls {where}
                GROUP BY tool ORDER BY calls DESC""",
            tuple(params),
        ).fetchall()
    out = []
    for r in rows:
        calls, ok = int(r["calls"]), int(r["ok"])
        out.append(
            {
                "tool": r["tool"],
                "calls": calls,
                "ok": ok,
                "errors": calls - ok,
                "success_rate": (ok / calls) if calls else 0.0,
                "avg_latency_ms": float(r["avg_latency_ms"])
                if r["avg_latency_ms"] is not None
                else None,
            }
        )
    return out


#: Session-duration histogram upper bounds in seconds (``+Inf`` is implicit).
DURATION_BUCKETS_S = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0)


def agent_metrics_rollup() -> dict[str, Any]:
    """Aggregates for the Prometheus exposition — every label bounded, none per-session.

    Labels are the logical ``agent`` name, the ``status`` outcome and the ``tool`` name: all drawn
    from small closed sets. A session id, a tenant or an args digest is never a label — those grow
    without bound and would turn the scrape into a cardinality incident.
    """
    init_db()
    out: dict[str, Any] = {}
    with get_db() as conn:
        out["sessions"] = [
            dict(r)
            for r in conn.execute(
                """SELECT COALESCE(agent,'unknown') AS agent, status,
                          COUNT(*) AS started,
                          SUM(CASE WHEN ended_at IS NOT NULL THEN 1 ELSE 0 END) AS ended,
                          COALESCE(SUM(steps),0) AS steps,
                          COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(cost_usd),0) AS cost_usd
                   FROM agent_sessions GROUP BY 1, 2"""
            ).fetchall()
        ]
        out["tools"] = [
            dict(r)
            for r in conn.execute(
                """SELECT tool, COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok
                   FROM agent_tool_calls GROUP BY tool"""
            ).fetchall()
        ]
        anomaly_rows = conn.execute(
            "SELECT COALESCE(agent,'unknown') AS agent, anomalies FROM agent_sessions "
            "WHERE anomalies IS NOT NULL"
        ).fetchall()
        durations = [
            (r["agent"], float(r["d"]))
            for r in conn.execute(
                """SELECT COALESCE(agent,'unknown') AS agent,
                          (julianday(ended_at) - julianday(started_at)) * 86400.0 AS d
                   FROM agent_sessions WHERE ended_at IS NOT NULL"""
            ).fetchall()
            if r["d"] is not None
        ]
        out["breaker"] = [
            dict(r)
            for r in conn.execute(
                """SELECT action, target, COUNT(*) AS n FROM audit_events
                   WHERE source='agentops' AND action IN
                         ('agent_breaker_warning','agent_breaker_tripped')
                   GROUP BY action, target"""
            ).fetchall()
        ]
    anomalies: dict[tuple[str, str], int] = {}
    for r in anomaly_rows:
        try:
            codes = json.loads(r["anomalies"])
        except (ValueError, TypeError):
            continue
        for code in codes if isinstance(codes, list) else []:
            key = (r["agent"], str(code))
            anomalies[key] = anomalies.get(key, 0) + 1
    out["anomalies"] = [{"agent": a, "code": c, "n": n} for (a, c), n in sorted(anomalies.items())]
    hist: dict[str, dict[str, Any]] = {}
    for agent, d in durations:
        h = hist.setdefault(agent, {"buckets": [0] * len(DURATION_BUCKETS_S), "sum": 0.0, "n": 0})
        h["sum"] += max(d, 0.0)
        h["n"] += 1
        for i, bound in enumerate(DURATION_BUCKETS_S):
            if d <= bound:
                h["buckets"][i] += 1
    out["durations"] = hist
    return out


install_write_retry(__name__)

"""examlops.data.economics — read-only per-kind ledger aggregates (ADR 0148 decision 4).

The three workload kinds already write to three different ledgers; this module only *reads*
them, one aggregate per kind, over an optional time window (``since`` = a ``YYYY-MM-DD HH:MM:SS``
UTC lower bound, the format ``CURRENT_TIMESTAMP`` writes). It never invents a value: a ledger
with no rows in the window yields ``n == 0`` and NULL-derived fields are returned as ``None``.
Policy (unit costs, minimum samples, what is unmetered) lives in
:mod:`examlops.finops.economics`.

* predictive -> ``predictions`` (+ ``inference_energy`` when present)
* generative -> ``gateway_calls`` (+ ``reasoning_usage``)
* agentic    -> ``agent_sessions`` (+ ``agent_tool_calls``)
"""

from __future__ import annotations

from typing import Any

from examlops.platform_db import get_db, init_db, write_retry

__all__ = ["agentic_ledger", "generative_ledger", "predictive_ledger"]


def _win(col: str, since: str | None) -> tuple[str, list[Any]]:
    return (f" AND {col} >= ?", [since]) if since else ("", [])


def predictive_ledger(since: str | None = None) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        init_db()
        w, a = _win("ts", since)
        with get_db() as conn:
            n = conn.execute(f"SELECT COUNT(*) FROM predictions WHERE 1=1{w}", a).fetchone()[0]
            e = conn.execute(
                "SELECT COUNT(*), SUM(requests), SUM(kwh), SUM(co2e_g), "
                f"SUM(CASE WHEN kwh IS NOT NULL THEN requests END) "
                f"FROM inference_energy WHERE 1=1{w}",
                a,
            ).fetchone()
        return {
            "predictions": int(n or 0),
            "energy_rows": int(e[0] or 0),
            "energy_requests": int(e[4] or 0),
            "kwh": e[2],
            "co2e_g": e[3],
        }

    return write_retry(_do)


def generative_ledger(since: str | None = None) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        init_db()
        w, a = _win("ts", since)
        with get_db() as conn:
            g = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN error=1 THEN 1 ELSE 0 END), "
                "SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_usd) "
                f"FROM gateway_calls WHERE 1=1{w}",
                a,
            ).fetchone()
            r = conn.execute(
                "SELECT COUNT(*), SUM(reasoning_tokens), SUM(output_tokens), "
                f"SUM(reasoning_cost), SUM(output_cost) FROM reasoning_usage WHERE 1=1{w}",
                a,
            ).fetchone()
        return {
            "calls": int(g[0] or 0),
            "errors": int(g[1] or 0),
            "prompt_tokens": int(g[2] or 0),
            "completion_tokens": int(g[3] or 0),
            "cost_usd": float(g[4] or 0.0),
            "reasoning_rows": int(r[0] or 0),
            "reasoning_tokens": int(r[1] or 0),
            "reasoning_output_tokens": int(r[2] or 0),
            "reasoning_cost": float(r[3] or 0.0),
            "reasoning_output_cost": float(r[4] or 0.0),
        }

    return write_retry(_do)


def agentic_ledger(since: str | None = None) -> dict[str, Any]:
    """Sessions in the window. Only *ended* sessions are tasks: an open one has no outcome yet."""

    def _do() -> dict[str, Any]:
        init_db()
        w, a = _win("started_at", since)
        with get_db() as conn:
            s = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN ended_at IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN ended_at IS NOT NULL AND status='ok' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN ended_at IS NOT NULL THEN cost_usd ELSE 0 END), "
                "SUM(CASE WHEN ended_at IS NOT NULL AND status='ok' THEN cost_usd ELSE 0 END), "
                "SUM(CASE WHEN ended_at IS NOT NULL THEN tool_calls ELSE 0 END), "
                "SUM(CASE WHEN ended_at IS NOT NULL THEN input_tokens + output_tokens ELSE 0 END)"
                f" FROM agent_sessions WHERE 1=1{w}",
                a,
            ).fetchone()
        return {
            "sessions": int(s[0] or 0),
            "tasks": int(s[1] or 0),
            "succeeded": int(s[2] or 0),
            "model_cost_usd": float(s[3] or 0.0),
            "succeeded_model_cost_usd": float(s[4] or 0.0),
            "tool_calls": int(s[5] or 0),
            "tokens": int(s[6] or 0),
        }

    return write_retry(_do)

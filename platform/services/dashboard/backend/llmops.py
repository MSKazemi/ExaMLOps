"""LLMOps console aggregators (F10 / ADR 0064).

View-shaped read helpers over the LLM-serving substrate: the ``llm_endpoints`` registry (engine /
HF model / parallelism / dtype) and the continuous-eval tables (``eval_runs`` / ``eval_results``).
They back the LLMOps console's endpoint registry + eval-scores views.

**Graceful degradation (F10 R6).** Backends not yet wired (prompt registry, gateway, cache, RAG,
vector DB) surface as empty "not-yet-available" sections rather than errors; missing tables yield
empty payloads, never a 500.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dbconn import connect


def _connect(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ── LLM endpoint registry (F10 R1/R2) ─────────────────────────────────────────


def endpoints(db_path: str) -> dict[str, Any]:
    """The LLM endpoint registry: engine / HF model / parallelism / dtype / enabled (F10 R2)."""
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "llm_endpoints"):
            return {"rows": [], "count": 0}
        rows = [
            {
                "model": r["model"],
                "engine": r["engine"],
                "hfModelId": r["hf_model_id"],
                "maxModelLen": r["max_model_len"],
                "tensorParallel": r["tensor_parallel_size"],
                "dtype": r["dtype"],
                "enabled": bool(r["enabled"]),
            }
            for r in conn.execute(
                "SELECT model, engine, hf_model_id, max_model_len, tensor_parallel_size, dtype, enabled "
                "FROM llm_endpoints ORDER BY model"
            )
        ]
        return {"rows": rows, "count": len(rows)}
    finally:
        conn.close()


# ── continuous-eval scores (F10 R1 / C2) ──────────────────────────────────────


def eval_summary(db_path: str) -> dict[str, Any]:
    """Latest eval run per model with its metric results + pass rate (F10 R1, C2).

    Returns one entry per model that has an eval run: the newest run's suite/status plus its metric
    rows (value, baseline, passed). ``passRate`` is the fraction of metrics that passed.
    """
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "eval_runs"):
            return {"models": [], "count": 0}
        # newest run id per model, then look up that run's suite/status by id
        latest = conn.execute(
            "SELECT r.model AS model, r.id AS run_id, r.suite AS suite, r.status AS status "
            "FROM eval_runs r "
            "JOIN (SELECT model, MAX(id) AS max_id FROM eval_runs GROUP BY model) latest "
            "  ON r.id = latest.max_id "
            "ORDER BY r.model"
        ).fetchall()

        has_results = _table_exists(conn, "eval_results")
        models: list[dict[str, Any]] = []
        for r in latest:
            metrics = []
            if has_results:
                metrics = [
                    {
                        "metric": m["metric"],
                        "value": round(m["value"], 4),
                        "baseline": round(m["baseline"], 4) if m["baseline"] is not None else None,
                        "passed": bool(m["passed"]),
                    }
                    for m in conn.execute(
                        "SELECT metric, value, baseline, passed FROM eval_results "
                        "WHERE eval_run_id = ? ORDER BY metric",
                        (r["run_id"],),
                    )
                ]
            passed = sum(1 for m in metrics if m["passed"])
            models.append(
                {
                    "model": r["model"],
                    "suite": r["suite"],
                    "status": r["status"],
                    "metrics": metrics,
                    "passRate": round(passed / len(metrics), 3) if metrics else None,
                }
            )
        return {"models": models, "count": len(models)}
    finally:
        conn.close()


# ── calculation methodology (ADR 0083) ────────────────────────────────────────


def calculation_providers() -> dict[str, Any]:
    """Which provider computes each LLMOps figure, with its ``ProviderMeta`` (ADR 0083).

    One row per domain (``llm_cost`` / ``llm_cache`` / ``llm_routing`` / ``rag_quality``), resolved
    exactly as the platform paths resolve it (``EXAMLOPS_<DOMAIN>_PROVIDER`` → ``providers.yaml``
    → registered default), so the console shows the formula that actually ran instead of a
    hardcoded methodology string. Without the ``examlops`` library (a slim image) the section is
    empty and ``available`` is false — degraded, never a 500 (F10 R6).
    """
    try:
        from examlops.llmops_providers import describe_active_providers
    except ImportError:
        return {"rows": [], "count": 0, "available": False}
    rows = describe_active_providers()
    return {"rows": rows, "count": len(rows), "available": True}

"""examlops.data.evaluation — Evaluation suites + gates.

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
    "get_eval_gate",
    "get_eval_results",
    "get_gate_reports",
    "list_perf_estimates",
    "record_eval_result",
    "record_gate_report",
    "record_perf_estimate",
    "set_eval_gate",
]


def get_eval_gate(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM eval_gates WHERE model=?", (model,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json"))
    return d


def get_eval_results(
    model: str, suite: str | None = None, *, alias: str | None = None
) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM eval_suite_results WHERE model=?"
    params: list[Any] = [model]
    if suite:
        q += " AND suite=?"
        params.append(suite)
    if alias:
        q += " AND alias=?"
        params.append(alias)
    q += " ORDER BY ts DESC"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_gate_reports(model: str, limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gate_reports WHERE model=? ORDER BY ts DESC LIMIT ?", (model, limit)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["report"] = json.loads(d.pop("report_json"))
        out.append(d)
    return out


def list_perf_estimates(model: str, *, last_n: int = 50) -> list[dict[str, Any]]:
    """Estimated-vs-realized performance history for a model (R3/R4)."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM perf_estimates WHERE model=? ORDER BY ts DESC, id DESC LIMIT ?",
            (model, last_n),
        ).fetchall()
    return [dict(r) for r in rows]


def record_eval_result(
    suite: str,
    model: str,
    scores: dict[str, float],
    *,
    run_id: str,
    model_version: str | None = None,
    alias: str | None = None,
    sample_size: int = 0,
    judge: dict[str, str] | None = None,
    dataset_revision: str | None = None,
) -> None:
    """Persist per-metric suite scores; idempotent per (suite, version, run, metric) (R7/R8)."""
    init_db()
    judge_model = (judge or {}).get("model")
    judge_prompt = (judge or {}).get("prompt_version")
    with get_db() as conn:
        for metric, score in scores.items():
            conn.execute(
                """INSERT OR IGNORE INTO eval_suite_results
                       (suite, model, model_version, alias, metric, score, sample_size,
                        judge_model, judge_prompt_version, dataset_revision, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    suite,
                    model,
                    model_version,
                    alias,
                    metric,
                    float(score),
                    sample_size,
                    judge_model,
                    judge_prompt,
                    dataset_revision,
                    run_id,
                ),
            )


def record_gate_report(
    model: str,
    passed: bool,
    mode: str,
    report: dict[str, Any],
    *,
    candidate: str | None = None,
    baseline: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO gate_reports (model, candidate, baseline, passed, mode, report_json)
               VALUES (?,?,?,?,?,?)""",
            (model, candidate, baseline, 1 if passed else 0, mode, json.dumps(report)),
        )


def record_perf_estimate(
    model: str,
    metric: str,
    *,
    estimated: float | None = None,
    realized: float | None = None,
    baseline: float | None = None,
    method: str = "cbpe-like",
) -> None:
    """Store a label-free performance estimate (or realized backfill) (R3)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO perf_estimates (model, metric, estimated, realized, baseline, method)
               VALUES (?,?,?,?,?,?)""",
            (model, metric, estimated, realized, baseline, method),
        )


def set_eval_gate(
    model: str,
    suite: str,
    metrics: list[dict[str, Any]],
    *,
    baseline_alias: str = "Production",
    mode: str = "block",
    updated_by: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO eval_gates (model, suite, baseline_alias, metrics_json, mode, updated_by)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(model) DO UPDATE SET
                   suite=excluded.suite, baseline_alias=excluded.baseline_alias,
                   metrics_json=excluded.metrics_json, mode=excluded.mode,
                   updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP""",
            (model, suite, baseline_alias, json.dumps(metrics), mode, updated_by),
        )


install_write_retry(__name__)

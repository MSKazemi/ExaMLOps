"""examlops.data.evaluation — Evaluation suites + gates.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "get_calibration_by_id",
    "get_eval_gate",
    "get_eval_results",
    "get_gate_reports",
    "get_judge_calibration",
    "list_judge_calibrations",
    "list_perf_estimates",
    "record_eval_result",
    "record_gate_report",
    "record_judge_calibration",
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
            # `, id DESC` like `list_perf_estimates` below and the dashboard's `eval_gate_state`:
            # two gate reports can land in one second (a promote and an autopilot cycle, a CI
            # matrix), and this list is read newest-first — the MCP `gate_reports` tool hands it
            # to an agent, which takes the first entry as the standing verdict.
            "SELECT * FROM gate_reports WHERE model=? ORDER BY ts DESC, id DESC LIMIT ?",
            (model, limit),
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
    calibration_id: str | None = None,
    non_proportion_metrics: Iterable[str] = (),
) -> None:
    """Persist per-metric suite scores; idempotent per (suite, version, run, metric) (R7/R8).

    Every row carries the judge's ``calibration_id`` (ADR 0111 G7.3 — evaluator provenance) and
    a Wilson interval around the score (G7.4 — no point values). The interval is computed only
    for scores that are proportions over a known sample; a raw RMSE gets none, because a Wilson
    interval on it would be a fabricated number.

    ``non_proportion_metrics`` names the metrics that carry a **unit** rather than a share, and
    it exists because the range test alone cannot tell them apart. A Wilson interval is defined
    for *k successes out of n trials*, so it says nothing about a duration or a price — yet
    ``latency_p50 = 0.01`` seconds and ``cost_usd = 0.0225`` both land inside ``[0, 1]`` and were
    silently given one, claiming a p50 latency of 0.01 s might really be 0.45 s. Worse, the same
    metric acquired an interval or not depending on the value it happened to take: a slow agent's
    ``latency_p50 = 2.5`` fell outside the range and got none, so one metric's own series was
    internally inconsistent. Declaring the unit-bearing names is explicit where the range test
    guessed, and it fails closed — an undeclared metric keeps the old behaviour rather than
    losing an interval it was entitled to.
    """
    from examlops.evaluation.calibration import wilson_interval

    init_db()
    judge_model = (judge or {}).get("model")
    judge_prompt = (judge or {}).get("prompt_version")
    if calibration_id is None and judge_model:
        latest = get_judge_calibration(judge_model, version=judge_prompt)
        calibration_id = (latest or {}).get("calibration_id")
    unitless = set(non_proportion_metrics)
    with get_db() as conn:
        for metric, score in scores.items():
            lo = hi = None
            if metric not in unitless and sample_size > 0 and 0.0 <= float(score) <= 1.0:
                lo, hi = wilson_interval(float(score) * sample_size, sample_size)
            conn.execute(
                """INSERT OR IGNORE INTO eval_suite_results
                       (suite, model, model_version, alias, metric, score, sample_size,
                        judge_model, judge_prompt_version, dataset_revision, run_id,
                        calibration_id, score_lo, score_hi)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    calibration_id,
                    lo,
                    hi,
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
    higher_is_better: bool | None = None,
    aggregate: str | None = None,
) -> None:
    """Configure a model's C3 regression gate.

    ``higher_is_better`` is the gate's own metric direction. ``None`` means *undeclared* — not
    False — and an undeclared gate falls back to whatever the caller passes, which is how every
    gate written before this parameter existed keeps its behaviour.

    ``aggregate`` is clause 5's policy ("all" | "majority"). ``None`` reads as ``all``, so a
    gate written before the column existed still blocks on any failing metric.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO eval_gates
                   (model, suite, baseline_alias, metrics_json, mode, updated_by,
                    higher_is_better, aggregate)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(model) DO UPDATE SET
                   suite=excluded.suite, baseline_alias=excluded.baseline_alias,
                   metrics_json=excluded.metrics_json, mode=excluded.mode,
                   updated_by=excluded.updated_by,
                   higher_is_better=excluded.higher_is_better,
                   aggregate=excluded.aggregate,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                model,
                suite,
                baseline_alias,
                json.dumps(metrics),
                mode,
                updated_by,
                None if higher_is_better is None else int(higher_is_better),
                aggregate,
            ),
        )


def record_judge_calibration(cal: Any) -> str:
    """Persist a :class:`~examlops.evaluation.calibration.JudgeCalibration`; returns its id.

    Calibrations are immutable measurements, never updated in place — a re-measurement is a new
    row with a new ``calibration_id``, so an old evaluation still resolves to the judge as it was
    when that evaluation ran (ADR 0111 G7.3).
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO judge_calibrations
                   (calibration_id, judge, version, kappa, kappa_lo, kappa_hi, position_bias,
                    test_retest, benchmarks, families, replications, paradox_flag,
                    sensitivity, specificity, n, at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cal.calibration_id,
                cal.judge,
                cal.version,
                float(cal.kappa),
                float(cal.kappa_ci[0]),
                float(cal.kappa_ci[1]),
                float(cal.position_bias),
                float(cal.test_retest),
                json.dumps(list(cal.benchmarks)),
                json.dumps(list(cal.families)),
                int(cal.replications),
                1 if cal.paradox_flag else 0,
                float(cal.sensitivity),
                float(cal.specificity),
                int(cal.n),
                cal.at,
            ),
        )
    return str(cal.calibration_id)


def get_judge_calibration(judge: str, *, version: str | None = None) -> dict[str, Any] | None:
    """The judge's most recent calibration, or None — and None means *not eligible*."""
    init_db()
    q = "SELECT * FROM judge_calibrations WHERE judge=?"
    params: list[Any] = [judge]
    if version:
        q += " AND version=?"
        params.append(version)
    q += " ORDER BY ts DESC, rowid DESC LIMIT 1"
    with get_db() as conn:
        row = conn.execute(q, params).fetchone()
    return dict(row) if row is not None else None


def get_calibration_by_id(calibration_id: str) -> dict[str, Any] | None:
    """Resolve the provenance handle stored on an evaluation result (G7.3)."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM judge_calibrations WHERE calibration_id=?", (calibration_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_judge_calibrations(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM judge_calibrations ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# Applied last so it also wraps the writers defined above (item 0.4).
install_write_retry(__name__)

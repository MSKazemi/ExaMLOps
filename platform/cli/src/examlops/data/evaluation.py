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
    "get_version_scores",
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


def get_version_scores(model: str, suite: str, version: str) -> dict[str, float]:
    """The newest score per metric for exactly ``model@version`` on ``suite``.

    The version is filtered **in SQL** — reading a model's whole evaluation history to keep one
    version's rows grows without bound. ``id DESC`` breaks a same-second tie (``ts`` has
    one-second resolution) toward the row written last.
    """
    init_db()
    out: dict[str, float] = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT metric, score FROM eval_suite_results "
            "WHERE model=? AND suite=? AND model_version=? ORDER BY ts DESC, id DESC",
            (model, suite, str(version)),
        ).fetchall()
    for row in rows:
        if row["metric"] not in out:
            out[row["metric"]] = float(row["score"])
    return out


def _is_agent_version(model: str, model_version: Any) -> bool:
    return model.startswith("agent-") and str(model_version or "").startswith("av-sha256:")


def get_eval_results_for_agent_version(
    agent: str, version_id: str, *, suite: str | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    """Evaluation results of one agent version (ADR 0146 d1), newest first, at most ``limit``.

    Matches the ``agent_version_id`` key, and - for rows written before that column existed -
    the same version recorded as ``model_version`` under the agent's ``agent-<name>`` key. The
    match is in SQL, before the ``LIMIT``, so a busy agent's other versions cannot crowd this
    version's rows out of the window.
    """
    init_db()
    q = (
        "SELECT * FROM eval_suite_results WHERE model=? "
        "AND (agent_version_id=? OR (agent_version_id IS NULL AND model_version=?))"
    )
    params: list[Any] = [f"agent-{agent}", version_id, version_id]
    if suite:
        q += " AND suite=?"
        params.append(suite)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
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
    tenant: str = "default",
    agent_version_id: str | None = None,
) -> None:
    """Persist per-metric suite scores; idempotent per (suite, version, run, metric) (R7/R8).

    ``agent_version_id`` keys the row to the agent version it measures (ADR 0146 d1). When it is
    not given it is derived: results recorded under an agent's registered-model name
    (``agent-<name>``) for a content-addressed version (``av-sha256:...``) are that version's.

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
    if agent_version_id is None and _is_agent_version(model, model_version):
        agent_version_id = str(model_version)
    with get_db() as conn:
        for metric, score in scores.items():
            lo = hi = None
            if metric not in unitless and sample_size > 0 and 0.0 <= float(score) <= 1.0:
                lo, hi = wilson_interval(float(score) * sample_size, sample_size)
            conn.execute(
                """INSERT OR IGNORE INTO eval_suite_results
                       (suite, model, model_version, alias, metric, score, sample_size,
                        judge_model, judge_prompt_version, dataset_revision, run_id,
                        calibration_id, score_lo, score_hi, tenant, agent_version_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    tenant or "default",
                    agent_version_id,
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


# ── ADR 0007 decisions 2/3 — online evaluation over live traffic ──────────────────────────


def recent_labelled_predictions(
    model: str, *, since: str, alias: str | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    """The newest labelled prediction per ``request_hash`` since ``since`` (UTC SQL timestamp).

    One row per request: the newest prediction for a hash, joined to the newest label for it. Every
    filter (model, alias, window) is in the SQL before the ``LIMIT``, so the bound never hides a
    row the filters would have kept. ``ORDER BY ts DESC, id DESC`` breaks one-second ties.
    """
    init_db()
    alias_sql = " AND alias=?" if alias else ""
    params: list[Any] = [model, since, *([alias] if alias else [])]
    q = (
        "SELECT p.request_hash, p.prediction, p.alias, p.ts, g.label "
        "FROM predictions p JOIN ground_truth g ON g.id = ("
        "  SELECT MAX(g2.id) FROM ground_truth g2 WHERE g2.request_hash = p.request_hash) "
        "WHERE p.id IN (SELECT MAX(id) FROM predictions WHERE model=? AND ts>=?"
        f"{alias_sql} GROUP BY request_hash) "
        "ORDER BY p.ts DESC, p.id DESC LIMIT ?"
    )
    params.append(int(limit))
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def has_eval_run(suite: str, model: str, run_id: str, *, tenant: str = "default") -> bool:
    """Whether any result is already recorded under ``run_id`` — the online window's idempotency.

    ``eval_suite_results``' UNIQUE key includes ``model_version``, and an online run records none
    (it scores an alias, not a version); SQLite treats NULLs as distinct in a UNIQUE index, so
    ``INSERT OR IGNORE`` alone would record a re-run of the same window twice.

    Scoped to ``tenant``: schedules are keyed ``(model, tenant)`` and the window run id carries no
    tenant, so an unscoped check let one tenant's recorded window mark another's as done.
    """
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM eval_suite_results WHERE suite=? AND model=? AND run_id=? "
            "AND tenant=? LIMIT 1",
            (suite, model, run_id, tenant or "default"),
        ).fetchone()
    return row is not None


def set_online_eval(
    model: str,
    *,
    suite: str,
    evaluators: list[str],
    source: str = "predictions",
    alias: str = "Production",
    sample_size: int = 50,
    window_s: int = 3600,
    judge_model: str | None = None,
    tenant: str = "default",
    updated_by: str | None = None,
) -> None:
    """Create or replace a model's online-eval schedule (re-enabling it). Idempotent."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO eval_online_config
                   (model, tenant, suite, source, evaluators, alias, sample_size, window_s,
                    judge_model, enabled, updated_by)
               VALUES (?,?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(model, tenant) DO UPDATE SET
                   suite=excluded.suite, source=excluded.source,
                   evaluators=excluded.evaluators, alias=excluded.alias,
                   sample_size=excluded.sample_size, window_s=excluded.window_s,
                   judge_model=excluded.judge_model, enabled=1,
                   updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP""",
            (
                model,
                tenant,
                suite,
                source,
                json.dumps(list(evaluators)),
                alias,
                int(sample_size),
                int(window_s),
                judge_model,
                updated_by,
            ),
        )


def disable_online_eval(
    model: str, *, tenant: str = "default", updated_by: str | None = None
) -> bool:
    """Stop scheduling a model's online eval; its config and history are kept. True if found."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE eval_online_config SET enabled=0, updated_by=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE model=? AND tenant=?",
            (updated_by, model, tenant),
        )
        return bool(cur.rowcount)


def _online_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["evaluators"] = json.loads(d.get("evaluators") or "[]")
    d["enabled"] = bool(d.get("enabled"))
    return d


def get_online_eval(model: str, *, tenant: str = "default") -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM eval_online_config WHERE model=? AND tenant=?", (model, tenant)
        ).fetchone()
    return _online_row(row) if row is not None else None


def list_online_evals(
    *, tenant: str | None = None, enabled_only: bool = False, limit: int = 500
) -> list[dict[str, Any]]:
    """Online-eval schedules; the tenant and enabled filters are applied before the LIMIT."""
    init_db()
    q = "SELECT * FROM eval_online_config WHERE 1=1"
    params: list[Any] = []
    if tenant is not None:
        q += " AND tenant=?"
        params.append(tenant)
    if enabled_only:
        q += " AND enabled=1"
    q += " ORDER BY tenant, model LIMIT ?"
    params.append(int(limit))
    with get_db() as conn:
        return [_online_row(r) for r in conn.execute(q, params).fetchall()]


def mark_online_eval_run(
    model: str, *, tenant: str, run_id: str, status: str, note: str = ""
) -> None:
    """Record the outcome of the latest cycle on the schedule row (what `status` shows)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE eval_online_config SET last_run_id=?, last_status=?, last_note=?, "
            "last_run_at=CURRENT_TIMESTAMP WHERE model=? AND tenant=?",
            (run_id, status, note[:500], model, tenant),
        )


def latest_eval_scores(
    limit: int = 500, *, model: str | None = None, tenant: str | None = "default"
) -> list[dict[str, Any]]:
    """The newest result per ``(tenant, suite, model, alias, metric)`` — the Prometheus gauges.

    Newest by ``(ts, id)``, so two runs in one second resolve to the later insert. Bounded: a
    gauge per series, never per run, and never more than ``limit`` series. The ``model`` and
    ``tenant`` filters are in the SQL, ahead of both the per-series pick and the ``LIMIT`` — a
    filter applied to the bounded result would silently drop a series the caller asked for.
    ``tenant=None`` means every tenant (each row carries its own).
    """
    init_db()
    where: list[str] = []
    params: list[Any] = []
    if model is not None:
        where.append("model=?")
        params.append(model)
    if tenant is not None:
        where.append("tenant=?")
        params.append(tenant)
    q = (
        "SELECT suite, model, alias, metric, score, score_lo, score_hi, sample_size, ts, tenant "
        "FROM (SELECT r.*, ROW_NUMBER() OVER ("
        "  PARTITION BY tenant, suite, model, COALESCE(alias,''), metric "
        "  ORDER BY ts DESC, id DESC) AS rn FROM eval_suite_results r"
        + (" WHERE " + " AND ".join(where) if where else "")
        + ") latest WHERE rn = 1 ORDER BY tenant, suite, model, alias, metric LIMIT ?"
    )
    params.append(int(limit))
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# Applied last so it also wraps the writers defined above (item 0.4).
install_write_retry(__name__)

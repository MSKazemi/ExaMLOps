"""examlops.data.finops — FinOps & Green-AI.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import datetime
from typing import Any  # noqa: F401

import examlops.platform_db as _pdb
from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "add_key_spend",
    "aggregate_model_costs",
    "get_carbon_records",
    "get_fairness_gates",
    "get_live_metrics",
    "get_model_costs",
    "join_predictions_with_truth",
    "record_model_cost",
    "set_fairness_gate",
    "total_gateway_cost",
    "write_carbon_record",
    "write_ground_truth",
    "write_live_metric",
    "write_prediction",
]


def add_key_spend(key_hash: str, cost_usd: float) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE virtual_keys SET spent_usd = spent_usd + ? WHERE key_hash=?",
            (cost_usd, key_hash),
        )


def aggregate_model_costs() -> list[dict[str, Any]]:
    """Per-model rollup of GPU-hours + cost across all recorded runs (for reporting, item 3.5)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT model_name, "
            "       COUNT(*) AS runs, "
            "       COALESCE(SUM(gpu_hours), 0) AS gpu_hours, "
            "       COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "  FROM model_costs GROUP BY model_name ORDER BY cost_usd DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_carbon_records(model: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM carbon_records"
    params: list[Any] = []
    if model is not None:
        sql += " WHERE model=?"
        params.append(model)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_fairness_gates(model: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM fairness_gates WHERE model=?", (model,)).fetchall()
    return [dict(r) for r in rows]


def get_live_metrics(model: str, alias: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM live_metrics WHERE model=?"
    params: list[Any] = [model]
    if alias is not None:
        sql += " AND alias=?"
        params.append(alias)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_model_costs(model_name: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM model_costs WHERE model_name=? ORDER BY version ASC, id ASC",
            (model_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def join_predictions_with_truth(model: str, alias: str | None = None) -> list[dict[str, Any]]:
    """Return prediction/label pairs for a model (optionally one alias).

    The join key is ``request_hash`` — a stable digest of the inference input the
    serving path already computes for drift logging. Feeds live-accuracy metrics
    (#9), the A/B stats engine (#13), canary analysis (#11) and eval (#14).
    """
    sql = (
        "SELECT p.model, p.alias, p.request_hash, p.prediction, g.label, g.source "
        "FROM predictions p JOIN ground_truth g ON p.request_hash = g.request_hash "
        "WHERE p.model=?"
    )
    params: list[Any] = [model]
    if alias is not None:
        sql += " AND p.alias=?"
        params.append(alias)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def record_model_cost(
    model_name: str,
    version: int,
    run_id: str | None,
    job_id: str | None,
    gpu_hours: float | None,
    cost_usd: float | None,
    project: str | None = None,
    cpu_hours: float | None = None,
) -> None:

    # Attribute the cost to the model's project (ADR 0086) when not passed explicitly.
    if project is None:
        project = _pdb.get_project_for_model(model_name)
    recorded_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_costs
               (model_name, version, run_id, job_id, gpu_hours, cost_usd, recorded_at, project,
                cpu_hours)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                model_name,
                version,
                run_id,
                job_id,
                gpu_hours,
                cost_usd,
                recorded_at,
                project,
                cpu_hours,
            ),
        )


def set_fairness_gate(
    model: str, sensitive_feature: str, metric: str, max_disparity: float
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fairness_gates
               (model, sensitive_feature, metric, max_disparity) VALUES (?,?,?,?)""",
            (model, sensitive_feature, metric, max_disparity),
        )


def total_gateway_cost(key_hash: str | None = None) -> float:
    init_db()
    with get_db() as conn:
        if key_hash:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS t FROM gateway_calls WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS t FROM gateway_calls"
            ).fetchone()
    return float(row["t"])


def write_carbon_record(
    model: str,
    run_id: str | None,
    kwh: float | None,
    co2e_g: float | None,
    grid_intensity: float | None = None,
    provider: str | None = None,
    signal_type: str | None = None,
    signal_method: str | None = None,
) -> None:
    """Persist one carbon figure, with the kind of signal that produced it (ADR 0112 decision 6).

    ``signal_type``/``signal_method`` default to ``None`` so every existing caller is unchanged;
    a row without them is a figure whose provenance was never recorded, which is visible rather
    than assumed.
    """
    with get_db() as conn:
        conn.execute(
            """INSERT INTO carbon_records
                   (run_id, model, kwh, co2e_g, grid_intensity, provider,
                    signal_type, signal_method)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, model, kwh, co2e_g, grid_intensity, provider, signal_type, signal_method),
        )


def write_ground_truth(request_hash: str, label: float, source: str = "manual") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ground_truth (request_hash, label, source) VALUES (?,?,?)",
            (request_hash, label, source),
        )


def write_live_metric(
    model: str,
    alias: str,
    metric: str,
    value: float,
    n: int = 0,
    window_start: str | None = None,
    window_end: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO live_metrics
               (model, alias, metric, value, n, window_start, window_end)
               VALUES (?,?,?,?,?,?,?)""",
            (model, alias, metric, value, n, window_start, window_end),
        )


def write_prediction(
    model: str,
    alias: str,
    request_hash: str,
    prediction: float,
    features_json: str | None = None,
    job_id: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO predictions
               (model, alias, request_hash, prediction, features_json, job_id)
               VALUES (?,?,?,?,?,?)""",
            (model, alias, request_hash, prediction, features_json, job_id),
        )


install_write_retry(__name__)

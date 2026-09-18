"""Prediction drift status per model, and the event when it changes (plan P2.4b).

One computation, used by ``exa drift status``, ``exa drift trigger``, the autopilot and the control
plane's evaluator. The autopilot used to keep its own copy: a 50-prediction window where the CLI
used 100, and hard-coded 2.0/3.0 thresholds that ignored a site's configured drift provider. The
same model at the same moment could be CRITICAL to one and OK to the other.

:func:`evaluate` compares each model's status with the one last recorded and, when it changed,
records the new one and emits ``drift.status_changed`` (and ``alert.drift`` for WARNING and
CRITICAL) in the same transaction: an event is never lost, never sent for a change that was not
recorded, and never sent twice by two evaluators, because the comparison runs under the write lock.
"""

from __future__ import annotations

import math
from typing import Any

from examlops.data.drift import (
    drift_models,
    get_drift_baseline,
    recent_drift_predictions,
    record_drift_statuses,
)
from examlops.data.events import enqueue_event
from examlops.drift_providers import resolve_drift_score_fn

SNAPSHOT_WINDOW = 100  # the most recent predictions a status is computed over
TREND_POINTS = 24  # recent predictions kept for the sparkline in `exa drift status`

_ALERT_SEVERITY = {"WARNING": "warn", "CRITICAL": "critical"}


def compute_stats(values: list[float]) -> dict[str, float]:
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return {"mean": mean, "std": math.sqrt(variance), "n": float(n)}


def model_rows(model_filter: str | None = None) -> list[dict[str, Any]]:
    """The drift status of every model with predictions (or of one), newest window first."""
    models = [model_filter] if model_filter else drift_models()
    score = resolve_drift_score_fn()
    results = []
    for model in models:
        preds = recent_drift_predictions(model, SNAPSHOT_WINDOW)
        if not preds:
            continue
        live = compute_stats(preds)
        baseline = get_drift_baseline(model)
        z, status = score(live["mean"], live["std"], baseline)
        results.append(
            {
                "model": model,
                "live_mean": round(live["mean"], 3),
                "live_std": round(live["std"], 3),
                "baseline_mean": round(baseline["mean"], 3) if baseline else None,
                "z_score": round(z, 2),
                "status": status,
                "n_snapshots": len(preds),
                # newest-first from the query; oldest→newest so a trend reads left to right
                "recent": [round(float(p), 3) for p in reversed(preds)][-TREND_POINTS:],
            }
        )
    return results


def model_row(model: str) -> dict[str, Any] | None:
    rows = model_rows(model)
    return rows[0] if rows else None


def record_transitions(
    rows: list[dict[str, Any]], *, actor: str = "control-plane"
) -> list[dict[str, Any]]:
    """Record each model's status; emit an event for every one that changed. Returns the changes.

    A model seen for the first time is recorded quietly when it is OK: nothing happened. Seen for
    the first time already WARNING or CRITICAL, it is announced with ``previous`` null.
    """
    return record_drift_statuses(rows, _announce, actor=actor)


def _announce(conn: Any, change: dict[str, Any], actor: str) -> bool:
    """Write the events for one change, on the recording transaction. False: nothing to announce."""
    status = change["status"]
    severity = _ALERT_SEVERITY.get(status.upper())
    if change["previous"] is None and severity is None:
        return False  # first sighting, nothing wrong: a baseline, not an event
    enqueue_event("drift.status_changed", change, conn=conn, actor=actor)
    if severity:
        enqueue_event(
            "alert.drift",
            {
                "kind": "prediction_drift",
                "target": change["model"],
                "severity": severity,
                "detail": f"{change['previous'] or 'new'} → {status} (z={change['z_score']})",
                "value": change["z_score"],
                "threshold": None,
            },
            conn=conn,
            actor=actor,
        )
    return True


def evaluate(*, actor: str = "control-plane") -> list[dict[str, Any]]:
    """Score every model and announce the ones whose status changed."""
    return record_transitions(model_rows(), actor=actor)

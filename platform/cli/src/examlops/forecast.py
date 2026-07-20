"""Predictive, pre-emptive drift forecasting + root-cause classification (Phase 5 item 5.2).

Today's autopilot is *reactive*: it retrains once drift has already crossed the critical threshold —
i.e. after the model has degraded. This module makes it *predictive*: fit a trend to the recent drift
signal, project **when** it will breach, and (if that's inside a lead-time window) let the autopilot
retrain *before* the degradation window opens. It also adds the missing "decide" stage — a root-cause
classifier that says *which* kind of drift is driving the breach (input-distribution vs prediction vs
data-volume), so the response can be targeted.

Pure functions over plain numbers → fully offline-testable; the drift-specific wrappers pull live
snapshots.
"""

from __future__ import annotations

from typing import Any


def linear_trend(series: list[float]) -> tuple[float, float]:
    """Ordinary least-squares slope + intercept of ``series`` against its index (0..n-1)."""
    n = len(series)
    if n < 2:
        return 0.0, (series[0] if series else 0.0)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0, mean_y
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, series)) / denom
    intercept = mean_y - slope * mean_x
    return slope, intercept


def forecast_breach(
    series: list[float],
    threshold: float,
    *,
    horizon: int = 20,
    higher_is_worse: bool = True,
) -> dict[str, Any]:
    """Project when a metric trend will breach ``threshold`` (item 5.2).

    Fits a linear trend to ``series`` (oldest→newest) and extrapolates. Returns
    ``{"will_breach", "eta_steps", "slope", "current", "projected_at_horizon"}``. ``eta_steps`` is
    how many steps ahead the trend crosses ``threshold`` (None if it won't within ``horizon`` or is
    trending the safe way). ``higher_is_worse`` flips the comparison for lower-is-better metrics.
    """
    if len(series) < 2:
        return {
            "will_breach": False,
            "eta_steps": None,
            "slope": 0.0,
            "reason": "insufficient data",
        }
    slope, intercept = linear_trend(series)
    n = len(series)
    current = series[-1]

    # Already breached?
    breached_now = current >= threshold if higher_is_worse else current <= threshold
    # Trending the safe direction → no future breach.
    worsening = slope > 0 if higher_is_worse else slope < 0

    eta = None
    if not breached_now and worsening:
        for step in range(1, horizon + 1):
            projected = intercept + slope * (n - 1 + step)
            crossed = projected >= threshold if higher_is_worse else projected <= threshold
            if crossed:
                eta = step
                break
    elif breached_now:
        eta = 0

    return {
        "will_breach": eta is not None,
        "eta_steps": eta,
        "slope": round(slope, 4),
        "current": round(current, 4),
        "projected_at_horizon": round(intercept + slope * (n - 1 + horizon), 4),
        "threshold": threshold,
    }


def classify_drift_cause(
    *,
    input_z: float | None = None,
    prediction_z: float | None = None,
    volume_ratio: float | None = None,
    z_warn: float = 2.0,
    volume_warn: float = 0.5,
) -> dict[str, Any]:
    """Root-cause "decide" stage (item 5.2): which drift dominates?

    - ``input_z``: input/embedding-distribution drift z-score (from `exa drift input`).
    - ``prediction_z``: output/prediction drift z-score (from `exa drift`).
    - ``volume_ratio``: recent data volume ÷ baseline (``<1`` = shrinking feed).

    Returns ``{"cause", "confidence", "signals"}`` where ``cause`` ∈ input_distribution |
    prediction_shift | data_volume | concept (both input+prediction) | none. This lets the autopilot
    pick a targeted response (e.g. re-baseline features vs full retrain) instead of always retraining.
    """
    signals = {}
    if input_z is not None:
        signals["input"] = abs(input_z) >= z_warn
    if prediction_z is not None:
        signals["prediction"] = abs(prediction_z) >= z_warn
    if volume_ratio is not None:
        signals["volume"] = volume_ratio < volume_warn

    inp = signals.get("input", False)
    pred = signals.get("prediction", False)
    vol = signals.get("volume", False)

    if inp and pred:
        cause, conf = "concept", "high"  # both shifted → concept drift
    elif inp:
        cause, conf = "input_distribution", "medium"
    elif pred:
        cause, conf = "prediction_shift", "medium"
    elif vol:
        cause, conf = "data_volume", "medium"
    else:
        cause, conf = "none", "low"

    return {"cause": cause, "confidence": conf, "signals": signals}


def forecast_model_drift(
    model: str, *, threshold: float = 3.0, horizon: int = 20, window: int = 50
) -> dict[str, Any]:
    """Forecast a model's prediction-drift breach from its recent snapshots (item 5.2).

    Builds a rolling z-score series of recent predictions vs the stored baseline and forecasts when
    it will breach ``threshold``. Degrades to ``{"will_breach": False}`` with a reason when there's
    no baseline or too few snapshots.
    """
    from examlops.data import get_db, init_db
    from examlops.data.drift import get_drift_baseline

    init_db()
    baseline = get_drift_baseline(model)
    if not baseline or not baseline.get("std"):
        return {"will_breach": False, "eta_steps": None, "reason": "no baseline"}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT prediction FROM drift_snapshots WHERE model=? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (model, window),
        ).fetchall()
    preds = [r["prediction"] for r in reversed(rows)]  # oldest → newest
    if len(preds) < 4:
        return {"will_breach": False, "eta_steps": None, "reason": "insufficient snapshots"}
    mean, std = baseline["mean"], baseline["std"]
    # Rolling z of a trailing mean, so the series reflects the *trend* of drift, not per-point noise.
    zs: list[float] = []
    k = max(3, len(preds) // 5)
    for i in range(k, len(preds) + 1):
        window_mean = sum(preds[i - k : i]) / k
        zs.append(abs(window_mean - mean) / std)
    result = forecast_breach(zs, threshold, horizon=horizon, higher_is_worse=True)
    result["model"] = model
    result["samples"] = len(preds)
    return result

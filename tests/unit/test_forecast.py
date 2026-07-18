"""Predictive drift forecasting + root-cause classification (Phase 5 item 5.2).

Proves the pre-emptive engine: a worsening trend forecasts a future breach with an ETA, a safe/flat
trend does not, an already-breached series reports eta 0, and the root-cause classifier distinguishes
input-distribution / prediction / data-volume / concept drift.
"""

from __future__ import annotations

import pytest

from examlops.forecast import classify_drift_cause, forecast_breach, linear_trend


def test_linear_trend_slope():
    slope, intercept = linear_trend([1.0, 2.0, 3.0, 4.0])
    assert round(slope, 3) == 1.0
    assert round(intercept, 3) == 1.0


def test_forecast_predicts_future_breach():
    # z-score climbing 1.0→1.4 per step; threshold 3.0 → breach a few steps ahead.
    series = [1.0, 1.4, 1.8, 2.2]
    r = forecast_breach(series, 3.0, horizon=10)
    assert r["will_breach"] is True
    assert r["eta_steps"] is not None and r["eta_steps"] >= 1
    assert r["slope"] > 0


def test_forecast_no_breach_when_flat():
    r = forecast_breach([1.0, 1.0, 1.0, 1.0], 3.0, horizon=10)
    assert r["will_breach"] is False and r["eta_steps"] is None


def test_forecast_no_breach_when_improving():
    r = forecast_breach([2.5, 2.0, 1.5, 1.0], 3.0, horizon=10)
    assert r["will_breach"] is False  # trending away from the threshold


def test_forecast_already_breached_is_eta_zero():
    r = forecast_breach([2.0, 2.5, 3.1, 3.5], 3.0, horizon=10)
    assert r["will_breach"] is True and r["eta_steps"] == 0


def test_forecast_horizon_bounds_lookahead():
    # Very slow climb won't cross within a short horizon.
    series = [1.0, 1.01, 1.02, 1.03]
    assert forecast_breach(series, 3.0, horizon=5)["will_breach"] is False


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({"input_z": 3.0, "prediction_z": 3.0}, "concept"),
        ({"input_z": 3.0, "prediction_z": 0.5}, "input_distribution"),
        ({"input_z": 0.2, "prediction_z": 4.0}, "prediction_shift"),
        ({"volume_ratio": 0.2}, "data_volume"),
        ({"input_z": 0.1, "prediction_z": 0.1}, "none"),
    ],
)
def test_classify_drift_cause(kw, expected):
    assert classify_drift_cause(**kw)["cause"] == expected


def test_forecast_model_drift_no_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb
    from examlops.forecast import forecast_model_drift

    pdb.init_db()
    assert forecast_model_drift("JPCP")["reason"] == "no baseline"


def test_forecast_model_drift_projects_breach(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb
    from examlops.forecast import forecast_model_drift

    pdb.init_db()
    pdb.set_drift_baseline("JPCP", {"mean": 0.0, "std": 1.0})
    # Predictions drifting steadily away from the baseline mean → z-trend climbs.
    for i in range(30):
        pdb.write_drift_snapshot("JPCP", "Production", float(i) * 0.5, "job")
    r = forecast_model_drift("JPCP", threshold=3.0)
    assert r["model"] == "JPCP" and r["samples"] == 30
    assert "will_breach" in r

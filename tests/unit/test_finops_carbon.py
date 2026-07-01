from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finops.carbon import (  # noqa: E402
    budget_usage_ratio,
    co2e_grams,
    estimate_carbon,
    estimate_energy_kwh,
)


def test_energy_estimate_matches_formula():
    # 10 GPU-h × 400W/1000 × PUE 1.5 = 6.0 kWh
    assert estimate_energy_kwh(10.0, gpu_tdp_watts=400.0, pue=1.5) == pytest.approx(6.0)


def test_co2e_from_kwh():
    assert co2e_grams(6.0, grid_intensity_g_per_kwh=300.0) == pytest.approx(1800.0)


def test_estimate_carbon_roundtrip():
    est = estimate_carbon(10.0, gpu_tdp_watts=400.0, pue=1.5, grid_intensity_g_per_kwh=300.0)
    assert est["kwh"] == pytest.approx(6.0)
    assert est["co2e_g"] == pytest.approx(1800.0)


def test_negative_inputs_raise():
    with pytest.raises(ValueError):
        estimate_energy_kwh(-1.0)
    with pytest.raises(ValueError):
        co2e_grams(-1.0)


def test_budget_usage_ratio():
    assert budget_usage_ratio(50.0, 100.0) == 0.5
    assert budget_usage_ratio(150.0, 100.0) == 1.5
    assert budget_usage_ratio(10.0, None) is None  # no budget → nothing to enforce
    assert budget_usage_ratio(0.0, 0.0) == 0.0
    assert math.isinf(budget_usage_ratio(5.0, 0.0))

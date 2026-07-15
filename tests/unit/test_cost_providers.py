"""S5: the `cost` domain reuses the pluggable substrate (proves generality; zero behaviour change)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finops import cost  # noqa: E402
from examlops.finops.cost import estimate_cost_via_provider  # noqa: E402
from examlops.providers import get_provider, list_providers  # noqa: E402


def test_flat_rate_default_matches_legacy_math(monkeypatch):
    monkeypatch.setenv("GPU_COST_PER_HOUR", "2.50")
    # legacy: gpu_hours × 2.50
    out = estimate_cost_via_provider(10.0)
    assert out["cost_usd"] == pytest.approx(25.0)
    assert out["provider"] == "flat-rate"


def test_flat_rate_includes_cpu_hours():
    out = estimate_cost_via_provider(10.0, 100.0)  # 10×2.50 + 100×0.05 = 30.0
    assert out["cost_usd"] == pytest.approx(30.0)


def test_cost_providers_registered_and_discoverable():
    import examlops.finops.cost_providers  # noqa: F401

    names = {i.name for i in list_providers("cost")}
    assert {"flat-rate", "tiered-example"} <= names


def test_tiered_provider_applies_volume_discount():
    import examlops.finops.cost_providers  # noqa: F401

    p = get_provider("cost", "tiered-example")
    # 150 GPU-h, threshold 100, 20% discount over: 100×2.5 + 50×2.5×0.8 = 250 + 100 = 350
    out = p.compute(
        {"gpu_hours": 150, "gpu_rate": 2.5, "tier_threshold": 100, "tier_discount": 0.2}
    )
    assert out["cost_usd"] == pytest.approx(350.0)


def test_config_selects_cost_provider():
    out = estimate_cost_via_provider(
        150.0,
        config={
            "provider": "tiered-example",
            "coefficients": {"gpu_rate": 2.5, "tier_threshold": 100, "tier_discount": 0.2},
        },
    )
    assert out["provider"] == "tiered-example"
    assert out["cost_usd"] == pytest.approx(350.0)


def test_inline_expression_cost_formula():
    # a sysadmin's custom rate card, no code: flat $3/gpu-h with a $5 fixed job fee
    out = estimate_cost_via_provider(
        10.0,
        config={
            "provider": "expression",
            "coefficients": {"rate": 3.0, "fee": 5.0},
            "formulas": {"cost_usd": "gpu_hours * rate + fee"},
        },
    )
    assert out["cost_usd"] == pytest.approx(35.0)


def test_bad_cost_provider_degrades_to_default():
    out = estimate_cost_via_provider(10.0, provider="nope")
    assert out["provider"] == "flat-rate"
    assert out["cost_usd"] == pytest.approx(25.0)


def test_flat_rate_cost_pure_helper():
    assert cost.flat_rate_cost(10.0, gpu_rate=2.5) == pytest.approx(25.0)
    with pytest.raises(ValueError):
        cost.flat_rate_cost(-1.0)

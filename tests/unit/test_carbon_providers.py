"""S1: carbon adopts the pluggable provider substrate (zero behaviour change by default)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finops import carbon  # noqa: E402
from examlops.finops.carbon import estimate_carbon_via_provider  # noqa: E402
from examlops.providers import get_provider, list_providers  # noqa: E402

# ── the default provider reproduces the legacy math byte-for-byte ─────────────


def test_default_provider_matches_legacy_estimate_carbon():
    legacy = carbon.estimate_carbon(10.0)
    via = estimate_carbon_via_provider(10.0)
    assert via["kwh"] == pytest.approx(legacy["kwh"])
    assert via["co2e_g"] == pytest.approx(legacy["co2e_g"])
    assert via["provider"] == "green-ai-default"
    assert via["uncertainty"] == 0.30


def test_default_provider_is_the_carbon_default():
    import examlops.finops.carbon_providers  # noqa: F401 - register
    from examlops.providers import default_provider_name

    assert default_provider_name("carbon") == "green-ai-default"


def test_green_ai_default_registered_and_discoverable():
    import examlops.finops.carbon_providers  # noqa: F401

    names = {i.name for i in list_providers("carbon")}
    assert {"green-ai-default", "codecarbon-like", "ccf-like"} <= names


# ── alternative built-in methodologies ────────────────────────────────────────


def test_codecarbon_like_adds_cpu_and_ram_energy():
    import examlops.finops.carbon_providers  # noqa: F401

    p = get_provider("carbon", "codecarbon-like")
    # 10h × (400 GPU + 120 CPU + 32×0.3725 RAM)/1000 × 1.5 PUE
    watts = 400 + 120 + 32 * 0.3725
    assert p.compute({"gpu_hours": 10})["kwh"] == pytest.approx(10 * (watts / 1000) * 1.5)
    # strictly more energy than the GPU-only default
    assert p.compute({"gpu_hours": 10})["kwh"] > carbon.estimate_carbon(10.0)["kwh"]


def test_ccf_like_uses_energy_coefficient():
    import examlops.finops.carbon_providers  # noqa: F401

    p = get_provider("carbon", "ccf-like")
    # 10h × 0.4 kWh/gpu-h × 1.5 PUE = 6.0 kWh
    assert p.compute({"gpu_hours": 10})["kwh"] == pytest.approx(6.0)


# ── config-driven coefficient override flows through ──────────────────────────


def test_config_coefficients_override_defaults():
    out = estimate_carbon_via_provider(
        10.0, config={"coefficients": {"grid_intensity_g_per_kwh": 100.0}}
    )
    # default kWh (6.0) × overridden grid 100 = 600 gCO2e (vs 1800 at default 300)
    assert out["co2e_g"] == pytest.approx(600.0)


def test_explicit_override_beats_config_coefficient():
    out = estimate_carbon_via_provider(
        10.0,
        config={"coefficients": {"pue": 2.0}},
        pue=1.0,  # explicit arg wins over configured coefficient
    )
    assert out["kwh"] == pytest.approx(10 * (400 / 1000) * 1.0)


def test_select_alternative_provider_by_name():
    out = estimate_carbon_via_provider(10.0, provider="ccf-like")
    assert out["provider"] == "ccf-like"
    assert out["kwh"] == pytest.approx(6.0)


def test_bad_provider_degrades_to_default():
    # an unknown provider name must not crash a calculation — fall back to the default
    out = estimate_carbon_via_provider(10.0, provider="does-not-exist")
    assert out["provider"] == "green-ai-default"
    assert out["co2e_g"] == pytest.approx(1800.0)


# ── inline YAML/expression formula path (no code) ─────────────────────────────


def test_inline_expression_formula_changes_the_math():
    out = estimate_carbon_via_provider(
        10.0,
        config={
            "provider": "expression",
            "coefficients": {"pue": 1.0, "grid": 250.0, "tdp": 700.0},
            "formulas": {
                "kwh": "gpu_hours * (tdp / 1000) * pue",
                "co2e_g": "kwh * grid",
            },
            "metadata": {"uncertainty": 0.2, "methodology": "H100 custom"},
        },
    )
    assert out["kwh"] == pytest.approx(7.0)  # 10 × 0.7 × 1.0
    assert out["co2e_g"] == pytest.approx(1750.0)
    assert out["uncertainty"] == 0.2

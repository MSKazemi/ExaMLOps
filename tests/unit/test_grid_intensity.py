"""BL-004 — live grid carbon-intensity provider (`grid-live`) + graceful degradation.

The live signal degrades to the platform's static default when no endpoint is configured or a
fetch fails, so carbon accounting always works offline; the provider is byte-identical to
green-ai-default in that degraded state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finops import carbon, grid_intensity  # noqa: E402
from examlops.providers import get_provider  # noqa: E402

_DEFAULT = carbon.DEFAULT_GRID_INTENSITY_G_PER_KWH


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "EXAMLOPS_GRID_INTENSITY_URL",
        "EXAMLOPS_GRID_INTENSITY_ZONE",
        "EXAMLOPS_GRID_INTENSITY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    grid_intensity.clear_cache()
    yield
    grid_intensity.clear_cache()


# ------------------------------------------------------------- _extract_intensity


@pytest.mark.parametrize(
    "body,expected",
    [
        (123.0, 123.0),
        ({"carbonIntensity": 210}, 210.0),
        ({"intensity": 55.5}, 55.5),
        ({"value": 300}, 300.0),
        ({"data": {"carbonIntensity": 175}}, 175.0),  # nested one level
        ({"nothing": "here"}, None),
        (True, None),  # bool guarded (int subclass)
        ({"carbonIntensity": True}, None),
    ],
)
def test_extract_intensity(body, expected):
    assert grid_intensity._extract_intensity(body) == expected


# ------------------------------------------------------------ current_grid_intensity


def test_no_url_returns_default():
    assert grid_intensity.current_grid_intensity(_DEFAULT) == _DEFAULT


def test_live_value_used_when_configured(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/intensity")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda url, timeout=5.0: 42.0)
    assert grid_intensity.current_grid_intensity(_DEFAULT) == 42.0


def test_fetch_failure_degrades_to_default(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/intensity")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda url, timeout=5.0: None)
    assert grid_intensity.current_grid_intensity(_DEFAULT) == _DEFAULT


def test_nonpositive_reading_degrades(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/intensity")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda url, timeout=5.0: 0.0)
    assert grid_intensity.current_grid_intensity(_DEFAULT) == _DEFAULT


def test_ttl_cache_fetches_once(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/intensity")
    calls = {"n": 0}

    def _counting_fetch(url, timeout=5.0):
        calls["n"] += 1
        return 88.0

    monkeypatch.setattr(grid_intensity, "_fetch", _counting_fetch)
    a = grid_intensity.current_grid_intensity(_DEFAULT)
    b = grid_intensity.current_grid_intensity(_DEFAULT)
    assert a == b == 88.0
    assert calls["n"] == 1  # second call served from the TTL cache


def test_zone_is_applied_to_url(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/i?fmt=json")
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_ZONE", "FR")
    seen = {}

    def _capture(url, timeout=5.0):
        seen["url"] = url
        return 10.0

    monkeypatch.setattr(grid_intensity, "_fetch", _capture)
    grid_intensity.current_grid_intensity(_DEFAULT)
    assert "zone=FR" in seen["url"]


def test_zone_placeholder_substitution(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/{zone}/now")
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_ZONE", "DE")
    seen = {}
    monkeypatch.setattr(
        grid_intensity, "_fetch", lambda url, timeout=5.0: seen.update(url=url) or 10.0
    )
    grid_intensity.current_grid_intensity(_DEFAULT)
    assert seen["url"] == "https://grid.example/DE/now"


# ----------------------------------------------------------------- the provider


def test_grid_live_provider_registered():
    import examlops.finops.carbon_providers  # noqa: F401 - register

    p = get_provider("carbon", "grid-live")
    assert p.name == "grid-live"


def test_grid_live_matches_default_when_offline():
    import examlops.finops.carbon_providers  # noqa: F401

    p = get_provider("carbon", "grid-live")
    # no endpoint configured → identical to the legacy/green-ai default
    assert p.compute({"gpu_hours": 10})["co2e_g"] == pytest.approx(
        carbon.estimate_carbon(10.0)["co2e_g"]
    )


def test_grid_live_uses_live_signal(monkeypatch):
    import examlops.finops.carbon_providers  # noqa: F401

    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/i")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda url, timeout=5.0: _DEFAULT * 2)
    grid_intensity.clear_cache()
    p = get_provider("carbon", "grid-live")
    # doubling grid intensity doubles CO2e vs the default
    assert p.compute({"gpu_hours": 10})["co2e_g"] == pytest.approx(
        carbon.estimate_carbon(10.0)["co2e_g"] * 2
    )


def test_explicit_grid_override_wins_over_live(monkeypatch):
    import examlops.finops.carbon_providers  # noqa: F401

    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "https://grid.example/i")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda url, timeout=5.0: 999.0)
    p = get_provider("carbon", "grid-live")
    out = p.compute({"gpu_hours": 10, "grid_intensity_g_per_kwh": _DEFAULT})
    assert out["co2e_g"] == pytest.approx(carbon.estimate_carbon(10.0)["co2e_g"])

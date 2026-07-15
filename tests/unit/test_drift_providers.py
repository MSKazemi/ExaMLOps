"""Unit tests for pluggable drift providers (INC-5 / ADR 0077).

Verifies:
* the default ``z-score`` provider is byte-identical to the previous inline logic;
* thresholds (WARN / CRIT) are respected;
* a declarative expression formula overrides scoring with zero core edits;
* graceful degradation when the provider fails;
* ``exa providers list`` sees the drift domain.
"""

from __future__ import annotations

import math

import pytest

from examlops.drift_providers import (
    DEFAULT_CRIT_Z,
    DEFAULT_WARN_Z,
    ZScoreDriftProvider,
    register_builtins,
    resolve_drift_score_fn,
)
from examlops.providers import default_provider_name, list_providers

# ── helper ────────────────────────────────────────────────────────────────────


def _baseline(mean: float, std: float) -> dict:
    return {"mean": mean, "std": std}


def _inline_z(live_mean, baseline):
    """Reference implementation — the pre-provider inline logic verbatim."""
    if baseline is None or baseline["std"] == 0.0:
        return 0.0, "OK (no baseline)"
    z = abs(live_mean - baseline["mean"]) / baseline["std"]
    if z >= DEFAULT_CRIT_Z:
        return z, "CRITICAL"
    if z >= DEFAULT_WARN_Z:
        return z, "WARNING"
    return z, "OK"


# ── ZScoreDriftProvider.compute ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "live_mean,bline,expected_status",
    [
        (1.0, _baseline(1.0, 1.0), "OK"),  # z = 0
        (1.0, _baseline(1.0, 0.0), "OK (no baseline)"),  # std=0 edge case
        (3.1, _baseline(0.0, 1.0), "CRITICAL"),  # z = 3.1 >= CRIT
        (2.5, _baseline(0.0, 1.0), "WARNING"),  # z = 2.5, WARN ≤ z < CRIT
        (1.5, _baseline(0.0, 1.0), "OK"),  # z = 1.5 < WARN
    ],
)
def test_z_score_provider_status(live_mean, bline, expected_status):
    p = ZScoreDriftProvider()
    inputs = {
        "live_mean": live_mean,
        "baseline_mean": bline["mean"],
        "baseline_std": bline["std"],
    }
    out = p.compute(inputs)
    assert out["status"] == expected_status


def test_z_score_provider_value_matches_inline():
    """Provider z-score must be byte-identical to the previous inline formula."""
    p = ZScoreDriftProvider()
    for live_mean in [0.5, 2.0, 5.0]:
        bline = _baseline(1.0, 0.5)
        out = p.compute(
            {"live_mean": live_mean, "baseline_mean": bline["mean"], "baseline_std": bline["std"]}
        )
        expected_z, _ = _inline_z(live_mean, bline)
        assert math.isclose(out["z_score"], expected_z), f"live_mean={live_mean}"


# ── resolve_drift_score_fn ────────────────────────────────────────────────────


def test_default_scorer_matches_inline_logic():
    """resolve_drift_score_fn() with no config == the pre-provider inline z-score."""
    score = resolve_drift_score_fn()
    cases = [
        (1.0, 0.1, _baseline(1.0, 0.5)),
        (5.0, 0.2, _baseline(1.0, 0.5)),
        (3.5, 0.3, _baseline(0.0, 1.0)),
    ]
    for live_mean, live_std, bline in cases:
        z_got, status_got = score(live_mean, live_std, bline)
        z_exp, status_exp = _inline_z(live_mean, bline)
        assert math.isclose(z_got, z_exp), f"z mismatch: live_mean={live_mean}"
        assert status_got == status_exp, f"status mismatch: live_mean={live_mean}"


def test_scorer_returns_no_baseline_when_baseline_is_none():
    score = resolve_drift_score_fn()
    z, status = score(5.0, 1.0, None)
    assert z == 0.0
    assert "no baseline" in status


def test_scorer_returns_no_baseline_when_std_is_zero():
    score = resolve_drift_score_fn()
    z, status = score(5.0, 1.0, _baseline(1.0, 0.0))
    assert z == 0.0
    assert "no baseline" in status


# ── expression formula overrides scoring (zero core edits) ───────────────────


def test_expression_formula_overrides_scoring():
    """A declarative formula returning always-critical passes through the substrate."""
    from examlops.providers import get_provider

    provider = get_provider(
        "drift",
        "expression",
        config={"formulas": {"z_score": "999.0", "status": "0"}},
    )
    # The formula outputs z_score=999, status=0; we just check the substrate wires it.
    out = provider.compute({"live_mean": 1.0, "baseline_mean": 1.0, "baseline_std": 1.0})
    assert float(out["z_score"]) == pytest.approx(999.0)


# ── graceful degradation ──────────────────────────────────────────────────────


def test_broken_provider_degrades_to_inline(monkeypatch):
    import examlops.drift_providers as mod

    monkeypatch.setattr(
        mod, "resolve_provider", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    score = mod.resolve_drift_score_fn()
    bline = _baseline(0.0, 1.0)
    z, status = score(3.5, 0.1, bline)
    # Should degrade to inline z-score
    assert z == pytest.approx(3.5)
    assert status == "CRITICAL"


def test_provider_compute_error_degrades(monkeypatch):
    import examlops.drift_providers as mod

    class Exploding:
        def compute(self, inputs):
            raise ValueError("bad formula")

    monkeypatch.setattr(mod, "resolve_provider", lambda *a, **k: Exploding())
    score = mod.resolve_drift_score_fn()
    bline = _baseline(0.0, 1.0)
    z, status = score(3.5, 0.1, bline)
    assert z == pytest.approx(3.5)
    assert status == "CRITICAL"


# ── registry ──────────────────────────────────────────────────────────────────


def test_registration_is_idempotent_and_default():
    register_builtins()
    register_builtins()  # idempotent
    names = {i.name for i in list_providers("drift")}
    assert "z-score" in names
    assert default_provider_name("drift") == "z-score"

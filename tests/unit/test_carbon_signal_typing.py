"""ADR 0112 — carbon signals are typed: accounting vs decision.

The decision rests on one verified sentence (Gorka, Rhodes & Roald, arXiv:2411.06560):
shifting on an *average* carbon metric can reduce the emissions **allocated** to the consumer
doing the shifting **while increasing the total emissions of the power system**. A greener
report and a worse world.

So the type is enforced rather than documented: reporting takes accounting, placement takes
decision, the wrong one raises, and when no decision signal exists the carbon objective's
weight is zero **and recorded** — no default is substituted, because substituting one here is
the harm itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.finops import carbon, grid_intensity
from examlops.finops.carbon_signal import (
    ACCOUNTING,
    DECISION,
    UNKNOWN,
    CarbonSignal,
    CarbonSignalTypeError,
    require_accounting,
    require_decision,
    signal_type_for_method,
)
from examlops.hpc_placement import ResourceAsk, carbon_objective_state, choose_cluster

runner = CliRunner()

AVERAGE = CarbonSignal(grams_per_kwh=300.0, method="average_grid_mix", zone="DE")
MARGINAL = CarbonSignal(grams_per_kwh=520.0, method="locational_marginal", zone="DE")
MYSTERY = CarbonSignal(grams_per_kwh=400.0, method="something_nobody_classified")


# ── the type ──────────────────────────────────────────────────────────────────


def test_type_is_derived_from_method_not_declared():
    """An operator who could label an average feed 'decision' would reintroduce exactly the
    error this exists to prevent — and the endpoint's number looks identical either way."""
    assert AVERAGE.signal_type == ACCOUNTING
    assert MARGINAL.signal_type == DECISION
    assert not hasattr(CarbonSignal, "signal_type_setter")
    with pytest.raises((AttributeError, TypeError)):
        AVERAGE.method = "locational_marginal"  # frozen: relabelling is not available


def test_an_unclassified_method_satisfies_neither_guard():
    """Absent beats inferred (P5), applied where substituting a plausible default is harmful."""
    assert MYSTERY.signal_type == UNKNOWN
    assert not MYSTERY.is_accounting and not MYSTERY.is_decision
    with pytest.raises(CarbonSignalTypeError):
        require_accounting(MYSTERY)
    with pytest.raises(CarbonSignalTypeError):
        require_decision(MYSTERY)


def test_signal_type_for_method_is_case_and_space_insensitive():
    assert signal_type_for_method("  LOCATIONAL_MARGINAL ") == DECISION
    assert signal_type_for_method("") == UNKNOWN


def test_every_figure_states_its_method_and_type():
    """Decision 6 — a carbon figure that does not say what kind it is can be quoted on a path
    where that kind is the wrong one."""
    d = AVERAGE.as_dict()
    assert d["signal_type"] == ACCOUNTING and d["method"] == "average_grid_mix"


# ── the guards ────────────────────────────────────────────────────────────────


def test_reporting_rejects_a_decision_signal():
    """A marginal signal overstates allocated emissions and would make EED reporting wrong in
    the other direction."""
    with pytest.raises(CarbonSignalTypeError, match="accounting"):
        carbon.estimate_emissions(10.0, MARGINAL)


def test_reporting_accepts_an_accounting_signal_and_states_it():
    out = carbon.estimate_emissions(10.0, AVERAGE)
    assert out["co2e_g"] > 0
    assert out["signal"]["signal_type"] == ACCOUNTING
    assert out["signal"]["method"] == "average_grid_mix"


def test_estimate_emissions_matches_estimate_carbon_arithmetically():
    """The typing must add provenance, not change the number — otherwise every historical
    figure silently becomes incomparable."""
    typed = carbon.estimate_emissions(10.0, AVERAGE, cpu_hours=3.0)
    plain = carbon.estimate_carbon(10.0, grid_intensity_g_per_kwh=300.0, cpu_hours=3.0)
    assert typed["kwh"] == plain["kwh"]
    assert typed["co2e_g"] == plain["co2e_g"]


def test_the_guard_raises_rather_than_warns():
    """Consistent with ADR 0111: a warning on a correctness-critical path is read past."""
    with pytest.raises(CarbonSignalTypeError):
        require_decision(AVERAGE)
    assert require_accounting(AVERAGE) is AVERAGE
    assert require_decision(MARGINAL) is MARGINAL


# ── placement ─────────────────────────────────────────────────────────────────

CLUSTERS = [
    {"name": "a", "scheduler": "slurm", "capabilities": {"total_gpus": 8, "total_nodes": 2}},
    {"name": "b", "scheduler": "flux", "capabilities": {"total_gpus": 4, "total_nodes": 1}},
]


def test_placement_without_a_decision_signal_zero_weights_and_says_so():
    """Decision 5 — the placement record names the missing objective. A zero weight that says
    nothing is indistinguishable from a bug."""
    res = choose_cluster(ResourceAsk(gpus=2), CLUSTERS)
    assert res.cluster == "a"
    assert res.objectives_unavailable == ["carbon"]
    assert res.objectives["carbon"]["weight"] == 0.0
    assert "no carbon signal available" in res.objectives["carbon"]["reason"]
    assert "objectives unavailable: carbon" in res.reason


def test_placement_refuses_to_use_an_accounting_signal():
    """The documented error: shifting on average intensity reduces allocated emissions while
    increasing the system's total. It is dropped, never used."""
    res = choose_cluster(ResourceAsk(gpus=2), CLUSTERS, carbon_signal=AVERAGE)
    assert res.objectives_unavailable == ["carbon"]
    assert "decision/marginal" in res.objectives["carbon"]["reason"]
    for c in res.candidates:
        assert "carbon_intensity_decision" not in c


def test_no_default_is_substituted_for_a_missing_carbon_signal():
    """P5 where substitution is actively harmful: the value must be absent, not defaulted."""
    res = choose_cluster(ResourceAsk(gpus=2), CLUSTERS, carbon_signal=None)
    assert "grams_per_kwh" not in res.objectives["carbon"]


def test_placement_uses_a_decision_signal():
    res = choose_cluster(ResourceAsk(gpus=2), CLUSTERS, carbon_signal=MARGINAL)
    assert res.objectives_unavailable == []
    assert res.objectives["carbon"]["signal_type"] == DECISION
    assert "objectives unavailable" not in res.reason


def test_strict_carbon_refuses_to_silently_place_carbon_blind():
    """A caller that asked for carbon-aware placement must not quietly get carbon-agnostic
    placement — that is the failure being silent about itself."""
    with pytest.raises(CarbonSignalTypeError):
        choose_cluster(ResourceAsk(gpus=2), CLUSTERS, carbon_signal=AVERAGE, strict_carbon=True)
    with pytest.raises(CarbonSignalTypeError):
        choose_cluster(ResourceAsk(gpus=2), CLUSTERS, carbon_signal=None, strict_carbon=True)


def test_carbon_objective_state_explains_itself():
    assert carbon_objective_state(None) == (False, "no carbon signal available")
    usable, reason = carbon_objective_state(MARGINAL)
    assert usable and "locational_marginal" in reason


def test_placement_behaviour_is_unchanged_when_no_carbon_is_asked_for():
    """The guard must not change which cluster is chosen for every existing caller."""
    res = choose_cluster(ResourceAsk(gpus=2), CLUSTERS)
    assert res.cluster == "a"
    assert [c["name"] for c in res.candidates] == ["a", "b"]


# ── the producer ──────────────────────────────────────────────────────────────


def test_a_fallback_is_never_dressed_up_as_a_live_reading(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_GRID_INTENSITY_URL", raising=False)
    grid_intensity.clear_cache()
    sig = grid_intensity.current_grid_signal(300.0)
    assert sig.method == grid_intensity.STATIC_METHOD
    assert sig.source == "fallback"
    assert sig.grams_per_kwh == 300.0
    assert sig.is_accounting and not sig.is_decision


def test_a_live_reading_carries_the_declared_method(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "http://grid.invalid/i")
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_METHOD", "locational_marginal")
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_ZONE", "DE")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda *a, **k: 512.0)
    grid_intensity.clear_cache()
    sig = grid_intensity.current_grid_signal(300.0)
    assert sig.grams_per_kwh == 512.0
    assert sig.is_decision
    assert sig.zone == "DE"


def test_an_unreachable_endpoint_degrades_to_an_accounting_fallback(monkeypatch):
    """Degrading must not hand placement a decision-typed signal it never actually read."""
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_URL", "http://grid.invalid/i")
    monkeypatch.setenv("EXAMLOPS_GRID_INTENSITY_METHOD", "locational_marginal")
    monkeypatch.setattr(grid_intensity, "_fetch", lambda *a, **k: None)
    grid_intensity.clear_cache()
    sig = grid_intensity.current_grid_signal(300.0)
    assert sig.method == grid_intensity.STATIC_METHOD
    assert not sig.is_decision
    assert carbon_objective_state(sig)[0] is False


def test_the_default_method_is_the_safe_one(monkeypatch):
    """Defaulting to 'decision' would make the harmful path the easy one."""
    monkeypatch.delenv("EXAMLOPS_GRID_INTENSITY_METHOD", raising=False)
    assert signal_type_for_method(grid_intensity.configured_method()) == ACCOUNTING


def test_the_float_accessor_still_works_for_every_existing_caller(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_GRID_INTENSITY_URL", raising=False)
    grid_intensity.clear_cache()
    assert grid_intensity.current_grid_intensity(300.0) == 300.0


# ── the CLI surface ───────────────────────────────────────────────────────────


def test_carbon_signal_command_reports_availability(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_GRID_INTENSITY_URL", raising=False)
    grid_intensity.clear_cache()
    result = runner.invoke(app, ["--json", "finops", "carbon", "signal"])
    assert result.exit_code == 0, result.output
    assert '"signal_type": "accounting"' in result.output
    assert '"usable_for_placement": false' in result.output


def test_carbon_estimate_states_its_signal():
    result = runner.invoke(app, ["--json", "finops", "carbon", "estimate", "--gpu-hours", "10"])
    assert result.exit_code == 0, result.output
    assert '"signal_type": "accounting"' in result.output
    assert '"method": "static_default"' in result.output


def test_an_operator_supplied_intensity_is_named_as_such():
    result = runner.invoke(
        app,
        ["--json", "finops", "carbon", "estimate", "--gpu-hours", "10", "--grid-intensity", "42"],
    )
    assert result.exit_code == 0, result.output
    assert '"method": "operator_supplied"' in result.output

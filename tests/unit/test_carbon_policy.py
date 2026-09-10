# tests/unit/test_carbon_policy.py
"""ADR 0112 R-ec / R-ed — carbon-aware placement must beat the simple baseline, and keep beating it.

The harness (`examlops.finops.carbon_policy`) is checked against hand-computed emissions, the
decision rule against the declared margin and retirement threshold, and the runtime gate end to
end: with no evaluation on record a carbon-first policy is replaced by the simple baseline, and
only a recorded, current, winning evaluation lets it place jobs on carbon.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finops import carbon_policy as cp  # noqa: E402

# Two regions, 6 hours. "clean" is cleaner on average; "home" dips at hours 3–4.
_TRACE = cp.Trace(
    intensity={"home": [400, 400, 400, 100, 100, 400], "clean": [200, 200, 200, 200, 200, 200]},
    method="marginal_operating_emissions_rate",
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in (
        "EXAMLOPS_CARBON_POLICY_GATE",
        "EXAMLOPS_CARBON_POLICY_MARGIN_PP",
        "EXAMLOPS_CARBON_POLICY_RETEST_DAYS",
        "EXAMLOPS_CARBON_POLICY_RETIRE_BELOW_PCT",
        "EXAMLOPS_PLACEMENT_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops.platform_db import init_db

    init_db()


def _job(**kw) -> cp.Job:
    base = {"id": "j", "submit": 0, "duration": 2, "energy_kwh": 2.0, "slack": 0, "home": "home"}
    return cp.Job(**{**base, **kw})


# ── the model ─────────────────────────────────────────────────────────────────


def test_emissions_match_the_hand_computed_formula():
    # 1 kWh per hour over hours 3–4 in `home`: 100 + 100 = 200 g
    assert cp.emissions_g(_job(), _TRACE, "home", 3) == pytest.approx(200.0)
    assert cp.emissions_g(_job(), _TRACE, "home", 0) == pytest.approx(800.0)


def test_each_policy_places_where_its_rule_says():
    job = _job(slack=3)
    assert cp.carbon_agnostic(job, _TRACE) == ("home", 0)
    assert cp.lowest_average_region(job, _TRACE) == ("clean", 0)  # mean 200 < 300
    # home sorted = [100,100,400,400,400,400]; its 30th percentile interpolates to
    # 100 + 0.5·300 = 250, and the first 2-hour window averaging <= 250 starts at hour 2
    assert cp.threshold_shift(job, _TRACE) == ("home", 2)
    assert cp.oracle(job, _TRACE) == ("home", 3)  # 200 g, the minimum
    rigid = _job(slack=0)
    assert cp.threshold_shift(rigid, _TRACE) == ("home", 0)  # no window qualifies: no waiting


def test_the_oracle_is_a_lower_bound_on_every_policy():
    data = cp.synthetic_trace(days=5, jobs=25, seed=3)
    jobs, trace = cp.load_trace(data)
    for job in jobs:
        best = cp.emissions_g(job, trace, *cp.oracle(job, trace))
        for policy in (
            cp.carbon_agnostic,
            cp.lowest_average_region,
            cp.threshold_shift,
            cp.forecast_greedy,
        ):
            assert cp.emissions_g(job, trace, *policy(job, trace)) >= best - 1e-9


def test_forecast_greedy_uses_only_what_it_could_know():
    # Yesterday hour 26 looked clean in `home`, today the dip moved to hour 29: the persistence
    # forecast picks 26, the oracle (which reads the future) picks 29.
    series = [400.0] * 48
    series[2] = 50.0  # yesterday, same hour as 26
    series[29] = 10.0
    trace = cp.Trace(intensity={"home": series})
    job = cp.Job("x", submit=25, duration=1, energy_kwh=1.0, slack=6, home="home")
    assert cp.forecast_greedy(job, trace) == ("home", 26)
    assert cp.oracle(job, trace) == ("home", 29)


# ── the decision (R-ec) and retirement (R-ed) ─────────────────────────────────


def test_a_policy_that_only_matches_the_simple_baseline_does_not_ship():
    jobs = [_job(id="a", slack=0)]
    # `carbon-aware` reads the current intensity: at hour 0 clean (200) < home (400) → same
    # choice as lowest-average-region, so it has no advantage at all.
    ev = cp.evaluate(jobs, _TRACE, "carbon-aware")
    assert ev.best_simple == "lowest-average-region"
    assert ev.advantage_pp == pytest.approx(0.0)
    assert ev.decision == "simple" and ev.shipped == "lowest-average-region"
    assert ev.reduction_pct["lowest-average-region"] == pytest.approx(50.0)  # 400 → 200 g


def test_a_policy_that_beats_the_margin_ships():
    jobs = [_job(id="a", slack=3)]
    ev = cp.evaluate(jobs, _TRACE, "forecast-greedy", margin=5.0)
    # agnostic 800 g; lowest-average-region 400 g (50 %) beats threshold-shift 500 g (37.5 %);
    # the oracle's 200 g (75 %) is the headroom. With no history before hour 0 the forecast is
    # flat, so on this tiny trace forecast-greedy has nothing to exploit.
    assert ev.best_simple == "lowest-average-region"
    assert ev.reduction_pct["threshold-shift"] == pytest.approx(37.5)
    assert ev.headroom_pct == pytest.approx(75.0)
    data = cp.synthetic_trace(days=14, jobs=60, seed=7)
    jobs2, trace2 = cp.load_trace(data)
    ev2 = cp.evaluate(jobs2, trace2, "forecast-greedy", margin=5.0)
    assert ev2.advantage_pp >= 5.0 and ev2.decision == "candidate"
    assert ev2.synthetic and any("synthetic" in n for n in ev2.notes)


def test_retirement_when_the_shipped_policy_no_longer_pays():
    flat = cp.Trace(intensity={"home": [300.0] * 6, "clean": [297.0] * 6})
    ev = cp.evaluate([_job()], flat, "carbon-aware", retire_below=2.0)
    assert ev.shipped_benefit_pct == pytest.approx(1.0)  # 600 → 594 g
    assert ev.retired is True


def test_evaluate_refuses_what_it_cannot_measure():
    with pytest.raises(ValueError):
        cp.evaluate([_job()], _TRACE, "lowest-average-region")  # a reference, not a candidate
    zero = cp.Trace(intensity={"home": [0.0] * 6})
    with pytest.raises(cp.TraceError):
        cp.evaluate([_job()], zero, "carbon-aware")
    with pytest.raises(cp.TraceError):
        cp.load_trace({"regions": {"a": [1.0, 2.0], "b": [1.0]}, "jobs": []})
    with pytest.raises(cp.TraceError):
        cp.evaluate([_job(submit=5)], _TRACE, "carbon-aware")  # does not fit the horizon


def test_accounting_traces_are_labelled_as_such():
    avg = cp.Trace(intensity=_TRACE.intensity, method="average_grid_mix")
    ev = cp.evaluate([_job()], avg, "carbon-aware")
    assert ev.signal_type == "accounting"
    assert any("accounting-basis" in n for n in ev.notes)


def test_recorded_evaluations_are_chained_and_read_back():
    from examlops.data.audit import verify_audit_chain

    ev = cp.evaluate([_job(slack=3)], _TRACE, "carbon-aware")
    cp.record_evaluation(ev, "tester")
    rows = cp.list_evaluations("carbon-aware")
    assert len(rows) == 1 and rows[0]["trace_digest"] == ev.trace_digest
    assert rows[0]["recorded_by"] == "tester" and rows[0]["event_id"]
    assert verify_audit_chain()["ok"]


# ── the gate ──────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _ev(**kw):
    base = {
        "candidate": "carbon-aware",
        "decision": "candidate",
        "best_simple": "lowest-average-region",
        "advantage_pp": 7.0,
        "margin_pp": 5.0,
        "retired": False,
        "shipped_benefit_pct": 40.0,
        "retire_below_pct": 2.0,
        "reduction_pct": {"lowest-average-region": 33.0},
        "synthetic": False,
        "evaluated_at": (_NOW - timedelta(days=10)).isoformat(),
        "event_id": 42,
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("evs", "primary", "action", "fragment"),
    [
        ([], True, "substitute", "no R-ec evaluation"),
        ([], False, "withhold", "no R-ec evaluation"),
        ([_ev(synthetic=True)], True, "substitute", "no R-ec evaluation"),
        (
            [_ev(evaluated_at=(_NOW - timedelta(days=120)).isoformat())],
            True,
            "substitute",
            "overdue",
        ),
        ([_ev(decision="simple", advantage_pp=1.2)], True, "substitute", "does not beat"),
        ([_ev(retired=True)], True, "agnostic", "retired"),
        ([_ev()], True, "allow", "beats"),
    ],
)
def test_gate_decisions(evs, primary, action, fragment):
    d = cp.gate_decision(
        "carbon-aware",
        carbon_primary=primary,
        evaluations=evs,
        now=_NOW,
        mode="enforce",
        cadence_days=90,
    )
    assert d.action == action and fragment in d.reason


def test_warn_mode_allows_but_records_what_enforce_would_do():
    d = cp.gate_decision(
        "carbon-aware", carbon_primary=True, evaluations=[], now=_NOW, mode="warn", cadence_days=90
    )
    assert d.action == "allow" and any("would have applied: substitute" in n for n in d.notes)


def test_the_simple_baseline_runs_unmeasured_but_retires_on_evidence():
    ok = cp.gate_decision(
        "carbon-simple",
        carbon_primary=True,
        evaluations=[],
        now=_NOW,
        mode="enforce",
        cadence_days=90,
    )
    assert ok.action == "allow" and any("unmeasured" in n for n in ok.notes)
    stale = _ev(reduction_pct={"lowest-average-region": 1.0})
    gone = cp.gate_decision(
        "carbon-simple",
        carbon_primary=True,
        evaluations=[stale],
        now=_NOW,
        mode="enforce",
        cadence_days=90,
    )
    assert gone.action == "agnostic" and "retired" in gone.reason


# ── the gate in placement ─────────────────────────────────────────────────────


def _cluster(name, *, idle_gpus, carbon=None, cost=None):
    caps = {"total_gpus": idle_gpus, "total_nodes": 2}
    if carbon is not None:
        caps["carbon_intensity"] = carbon
    if cost is not None:
        caps["cost_per_gpu_hour"] = cost
    return {"name": name, "scheduler": "flux", "capabilities": caps, "nodes": []}


def test_weighs_carbon_is_decided_by_probing():
    from examlops.hpc_placement_providers import (
        CarbonAwareProvider,
        LeastLoadedProvider,
        LowestIntensityProvider,
        weighs_carbon,
    )

    assert not weighs_carbon(LeastLoadedProvider())
    assert weighs_carbon(CarbonAwareProvider())
    assert weighs_carbon(LowestIntensityProvider())


def test_carbon_simple_ranks_unknown_intensity_last():
    from examlops.hpc_placement import ResourceAsk, choose_cluster
    from examlops.hpc_placement_providers import resolve_placement_score_fn

    clusters = [_cluster("unknown", idle_gpus=64), _cluster("known", idle_gpus=2, carbon=500)]
    fn = resolve_placement_score_fn("carbon-simple")
    assert choose_cluster(ResourceAsk(gpus=1), clusters, fn).cluster == "known"


def test_unmeasured_carbon_aware_placement_runs_the_simple_baseline():
    from examlops.hpc_placement import ResourceAsk, choose_cluster
    from examlops.hpc_placement_providers import resolve_placement_score_fn

    # carbon-aware would trade: 600 g but 32 idle GPUs (score 3100 - 1200 = 1900 > 80 - 80 = 0)…
    clusters = [
        _cluster("big-dirty", idle_gpus=32, carbon=600),
        _cluster("green", idle_gpus=2, carbon=40),
    ]
    fn = resolve_placement_score_fn("carbon-aware")
    assert fn.placement_policy["effective"] == "carbon-simple"
    assert fn.placement_policy["action"] == "substitute"
    result = choose_cluster(ResourceAsk(gpus=1), clusters, fn)
    assert result.cluster == "green"  # …the simple baseline just takes the greenest
    assert "carbon-aware → carbon-simple" in result.reason
    assert result.objectives["placement_policy"]["reason"].startswith("no R-ec evaluation")


def test_a_recorded_winning_evaluation_lets_the_policy_place_on_carbon(monkeypatch):
    from examlops.hpc_placement import ResourceAsk, choose_cluster
    from examlops.hpc_placement_providers import resolve_placement_score_fn

    ev = cp.evaluate(*cp.load_trace(cp.synthetic_trace()), "carbon-aware", margin=0.0)
    ev.synthetic = False  # stand-in for a real trace: synthetic evidence never gates
    ev.decision, ev.shipped = "candidate", "carbon-aware"
    cp.record_evaluation(ev, "tester")
    fn = resolve_placement_score_fn("carbon-aware")
    assert fn.placement_policy["action"] == "allow"
    clusters = [
        _cluster("big-dirty", idle_gpus=32, carbon=600),
        _cluster("green", idle_gpus=2, carbon=40),
    ]
    assert choose_cluster(ResourceAsk(gpus=1), clusters, fn).cluster == "big-dirty"


def test_a_non_primary_policy_keeps_its_other_objectives_when_carbon_is_withheld():
    from examlops.hpc_placement import ResourceAsk, choose_cluster
    from examlops.hpc_placement_providers import resolve_placement_score_fn

    fn = resolve_placement_score_fn("cost-aware")  # weighs carbon at 0.5, cost at 2.0
    assert fn.placement_policy["action"] == "withhold"
    # penalties (carbon 0.5·g, cost 2·$·100): pricey-green 5 + 400 = 405, cheap-dirty 450 + 100
    # = 550 → green wins while carbon counts; withheld, only cost counts (400 vs 100) → cheap.
    clusters = [
        _cluster("pricey-green", idle_gpus=4, carbon=10, cost=2.0),
        _cluster("cheap-dirty", idle_gpus=4, carbon=900, cost=0.5),
    ]
    assert choose_cluster(ResourceAsk(gpus=1), clusters, fn).cluster == "cheap-dirty"


# ── the CLI ───────────────────────────────────────────────────────────────────


def test_cli_sample_evaluate_record_status_list(tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.commands.carbon_policy_cmd import app

    runner = CliRunner()
    trace = tmp_path / "trace.json"
    assert (
        runner.invoke(app, ["sample", "--out", str(trace), "--days", "5", "--jobs", "12"]).exit_code
        == 0
    )
    assert json.loads(trace.read_text())["synthetic"] is True
    res = runner.invoke(app, ["evaluate", "carbon-aware", "--trace", str(trace), "--record"])
    assert res.exit_code == 0, res.output
    assert "simple policy ships" in res.output or "ships" in res.output
    listed = runner.invoke(app, ["list"])
    # Rich fixes the console width at import, so a narrow worker wraps the table: assert on the
    # row count and the stored evaluation, not on a cell's text.
    assert listed.exit_code == 0 and "1 item" in listed.output
    assert [r["candidate"] for r in cp.list_evaluations()] == ["carbon-aware"]
    status = runner.invoke(app, ["status", "carbon-aware"])
    assert (
        status.exit_code == 0 and "substitute" in status.output
    )  # synthetic evidence gates nothing
    bad = runner.invoke(
        app, ["evaluate", "carbon-aware", "--trace", str(tmp_path / "missing.json")]
    )
    assert bad.exit_code != 0


def test_a_withheld_formula_keeps_its_other_terms():
    # A YAML-style formula naming carbon_intensity must not break when carbon is withheld: the
    # carbon term is neutralised (equal for every cluster) and the cost term still decides.
    from examlops.hpc_placement import ResourceAsk, choose_cluster
    from examlops.hpc_placement_providers import _adapt, weighs_carbon
    from examlops.providers.yaml_provider import ExpressionProvider

    formula = ExpressionProvider(
        "green-and-cheap", {"score": "idle_gpus * 100 - carbon_intensity - cost_per_gpu_hour * 500"}
    )
    assert weighs_carbon(formula)
    clusters = [
        _cluster("dirty-cheap", idle_gpus=4, carbon=900, cost=0.1),
        _cluster("green-pricey", idle_gpus=4, carbon=10, cost=1.0),
    ]
    counted = choose_cluster(ResourceAsk(gpus=1), clusters, _adapt(formula, strip_carbon=False))
    withheld = choose_cluster(ResourceAsk(gpus=1), clusters, _adapt(formula, strip_carbon=True))
    assert counted.cluster == "green-pricey"  # 900 g outweighs a 450-point cost gap
    assert withheld.cluster == "dirty-cheap"  # carbon neutral → cost decides, formula intact

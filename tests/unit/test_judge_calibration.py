# tests/unit/test_judge_calibration.py
"""ADR 0111 — no uncalibrated judge may gate (G7.1–G7.4).

Acceptance from `design/vision/aidc/SPECS.md` §7:
GWT-1 a judge with position_bias 0.19 is refused by a promotion gate, and the failed check
is named · GWT-2 test_retest 0.99 with position_bias 0.19 sets paradox_flag and eligibility
is false · GWT-3 every evaluation result carries calibration_id, resolving to the judge's
kappa and bias at the time of that evaluation · GWT-4 no calibration ⇒
`(False, ["no_calibration"])` — absence is not eligibility.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.data.evaluation import (  # noqa: E402
    get_calibration_by_id,
    record_eval_result,
    record_judge_calibration,
)
from examlops.evaluation import calibration as cal_mod  # noqa: E402
from examlops.evaluation import gate as gate_mod  # noqa: E402
from examlops.platform_db import get_eval_results, init_db, set_eval_gate  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


def _cal(**over):
    """A calibration that passes every MVVP check unless a field is overridden."""
    base = dict(
        judge="judge-1",
        version="v1",
        at="2026-08-19T00:00:00+00:00",
        kappa=0.71,
        kappa_ci=(0.62, 0.80),
        position_bias=0.02,
        test_retest=0.88,
        benchmarks=["mt-bench-sample", "gsm8k-sample"],
        families=["correctness", "preference"],
        replications=3,
        paradox_flag=False,
        sensitivity=0.9,
        specificity=0.85,
        n=200,
    )
    base.update(over)
    return cal_mod.JudgeCalibration(**base)


# ── The statistics (G7.1, G7.4) ───────────────────────────────────────────────


def test_kappa_is_chance_corrected_not_raw_agreement():
    """On a skewed label set, raw agreement flatters the judge by tens of points.

    Here raw agreement is 0.90 and kappa is 0.615 — a 28.5 pp gap, the same phenomenon the
    ADR's evidence measures at 33.8-41.3 pp on MT-Bench. Reporting the 0.90 is the mistake
    G7.1 forbids; the exact size of the gap tracks the label distribution, not judge quality.
    """
    judge = [1] * 9 + [0]
    human = [1] * 8 + [0, 0]
    raw = sum(1 for a, b in zip(judge, human) if a == b) / len(judge)
    kappa, (lo, hi) = cal_mod.cohens_kappa(judge, human)
    assert raw == 0.9
    assert kappa == pytest.approx(0.6153846, abs=1e-6)
    assert raw - kappa > 0.25  # raw agreement inflates the judge by tens of points
    assert lo < kappa < hi


def test_kappa_of_perfect_disagreement_is_negative():
    kappa, _ = cal_mod.cohens_kappa([1, 1, 0, 0], [0, 0, 1, 1])
    assert kappa < 0


def test_wilson_interval_stays_inside_the_unit_interval_at_the_extremes():
    lo, hi = cal_mod.wilson_interval(10, 10)
    assert 0.0 <= lo <= hi <= 1.0
    assert lo < 1.0  # 10/10 is not certainty
    lo0, hi0 = cal_mod.wilson_interval(0, 10)
    assert lo0 == 0.0 or lo0 > 0.0
    assert hi0 < 1.0


def test_wilson_interval_narrows_with_sample_size():
    small = cal_mod.wilson_interval(8, 10)
    large = cal_mod.wilson_interval(800, 1000)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_position_bias_is_zero_for_an_order_blind_judge():
    assert cal_mod.position_bias([True, False] * 10) == 0.0


def test_position_bias_is_half_when_the_first_slot_always_wins():
    assert cal_mod.position_bias([True] * 10) == 0.5


def test_test_retest_needs_at_least_two_replications():
    assert cal_mod.test_retest([[1, 0, 1]]) == 0.0
    assert cal_mod.test_retest([[1, 0, 1], [1, 0, 1]]) == 1.0


def test_rogan_gladen_corrects_an_apparent_rate_and_refuses_a_chance_judge():
    # A judge with sens=spec=0.9 reporting 50% apparent -> 50% true (symmetric errors cancel).
    assert cal_mod.rogan_gladen(0.5, 0.9, 0.9) == pytest.approx(0.5, abs=1e-9)
    # An imperfect judge under-counts positives, so the corrected rate is HIGHER than the
    # apparent one: (0.8 + 0.9 - 1) / (0.9 + 0.9 - 1) = 0.875. The direction is the point —
    # the raw pass-rate is a biased estimate either way.
    assert cal_mod.rogan_gladen(0.8, 0.9, 0.9) == pytest.approx(0.875)
    assert cal_mod.rogan_gladen(0.2, 0.9, 0.9) < 0.2
    # A judge no better than chance carries no information — no number is returned.
    assert cal_mod.rogan_gladen(0.8, 0.5, 0.5) is None


def test_sensitivity_specificity_against_ground_truth():
    sens, spec = cal_mod.sensitivity_specificity([1, 1, 0, 0], [1, 0, 0, 0])
    assert sens == 1.0
    assert spec == pytest.approx(2 / 3)


# ── Eligibility rules (G7.2) ──────────────────────────────────────────────────


def test_a_fully_measured_judge_is_eligible():
    assert cal_mod.eligibility_failures(_cal()) == []


def test_gwt1_position_bias_over_the_threshold_is_refused_and_named():
    failures = cal_mod.eligibility_failures(_cal(position_bias=0.19))
    assert failures
    assert any("position_bias" in f for f in failures)


def test_gwt2_the_consistency_bias_paradox_is_a_hard_failure():
    """test-retest 0.99 with position bias 0.19: reproducible AND wrong."""
    c = _cal(test_retest=0.99, position_bias=0.19, paradox_flag=True)
    failures = cal_mod.eligibility_failures(c)
    assert c.paradox_flag is True
    assert any("paradox" in f for f in failures)


def test_fewer_than_three_replications_is_refused():
    assert any("replications" in f for f in cal_mod.eligibility_failures(_cal(replications=2)))


def test_one_benchmark_family_is_refused():
    failures = cal_mod.eligibility_failures(_cal(families=["correctness"]))
    assert any("benchmark_families" in f for f in failures)


def test_two_benchmarks_of_the_same_kind_do_not_satisfy_the_family_rule():
    failures = cal_mod.eligibility_failures(
        _cal(families=["correctness", "robustness"], benchmarks=["a", "b"])
    )
    assert any("missing benchmark family" in f for f in failures)


def test_kappa_without_an_interval_is_refused():
    assert any("interval" in f for f in cal_mod.eligibility_failures(_cal(kappa_ci=(0.0, 0.0))))


# ── Absence is not eligibility (G7.4 of the spec / decision 7) ─────────────────


def test_gwt4_an_unmeasured_judge_is_not_eligible():
    eligible, failures = cal_mod.is_gate_eligible("never-measured")
    assert eligible is False
    assert failures == ["no_calibration"]


def test_an_empty_judge_name_is_not_eligible():
    assert cal_mod.is_gate_eligible("") == (False, ["no_calibration"])


def test_a_recorded_calibration_makes_a_judge_eligible():
    record_judge_calibration(_cal(judge="good-judge"))
    eligible, failures = cal_mod.is_gate_eligible("good-judge")
    assert (eligible, failures) == (True, [])


# ── calibrate() over a live judge seam ────────────────────────────────────────


def test_calibrate_measures_a_perfect_judge_as_eligible():
    bench_a = cal_mod.CalibrationBenchmark(
        "corr",
        "correctness",
        [cal_mod.CalibrationItem(prompt=f"p{i}", human_label=i % 2) for i in range(20)],
    )
    bench_b = cal_mod.CalibrationBenchmark(
        "pref",
        "preference",
        [cal_mod.CalibrationItem(prompt=f"q{i}", human_label=i % 2) for i in range(20)],
    )

    def judge_fn(prompt: str) -> float:
        # Deterministic and correct: odd-indexed prompts are positives.
        return 1.0 if int(prompt[1:].split("\n")[0]) % 2 else 0.0

    c = cal_mod.calibrate(judge_fn, [bench_a, bench_b], judge="perfect", replications=3)
    assert c.kappa == pytest.approx(1.0)
    assert c.test_retest == 1.0
    assert c.replications == 3
    assert sorted(c.families) == ["correctness", "preference"]
    assert cal_mod.eligibility_failures(c) == []


def test_calibrate_from_records_infers_the_weakest_replication_count():
    records = {
        "benchmarks": [
            {
                "name": "b1",
                "family": "correctness",
                "items": [
                    {"human_label": 1, "judge_scores": [1, 1, 1]},
                    {"human_label": 0, "judge_scores": [0, 0]},  # only two replications
                ],
            }
        ]
    }
    c = cal_mod.calibrate_from_records(records, judge="j")
    assert c.replications == 2
    assert any("replications" in f for f in cal_mod.eligibility_failures(c))


def test_calibration_id_is_stable_for_the_same_measurement():
    assert _cal().calibration_id == _cal().calibration_id
    assert _cal().calibration_id != _cal(kappa=0.5).calibration_id


# ── The gate refuses (GWT-1, end to end) ──────────────────────────────────────


def _configure_gate(model="JPCP", suite="s"):
    set_eval_gate(
        model,
        suite,
        [{"name": "judge_score", "min": 0.5}],
        baseline_alias="Production",
        mode="block",
        updated_by="test",
    )


def test_gwt1_the_promotion_gate_refuses_a_biased_judge_and_names_the_check():
    _configure_gate()
    record_judge_calibration(_cal(judge="biased", position_bias=0.19))
    result = gate_mod.run_eval_gate(
        "JPCP",
        "7",
        candidate_scores={"judge_score": 0.95},
        baseline_scores={"judge_score": 0.9},
        judge="biased",
    )
    assert result is not None
    assert result.passed is False
    assert result.judge_eligible is False
    named = [m for m in result.metrics if m.name == "judge_calibration"]
    assert named and "position_bias" in named[0].reason


def test_an_unmeasured_judge_blocks_the_gate_even_though_the_metric_passes():
    _configure_gate()
    result = gate_mod.run_eval_gate(
        "JPCP", "7", candidate_scores={"judge_score": 0.99}, judge="ghost"
    )
    assert result is not None and result.passed is False
    assert result.judge_failures == ["no_calibration"]


def test_a_calibrated_judge_lets_the_gate_decide_on_the_metrics():
    _configure_gate()
    record_judge_calibration(_cal(judge="good"))
    result = gate_mod.run_eval_gate(
        "JPCP", "7", candidate_scores={"judge_score": 0.99}, judge="good"
    )
    assert result is not None and result.passed is True
    assert result.judge_eligible is True
    assert result.calibration_id


def test_warn_mode_still_refuses_an_ineligible_judge():
    """warn is advisory about metric regressions, never about an unmeasured instrument."""
    set_eval_gate("WarnModel", "s", [{"name": "judge_score", "min": 0.5}], mode="warn")
    result = gate_mod.run_eval_gate(
        "WarnModel", "1", candidate_scores={"judge_score": 0.1}, judge="ghost"
    )
    assert result is not None and result.passed is False


def test_a_deterministic_suite_has_no_judge_and_is_unaffected():
    _configure_gate(model="Deterministic")
    result = gate_mod.run_eval_gate(
        "Deterministic", "1", candidate_scores={"judge_score": 0.9}, judge=None
    )
    assert result is not None and result.passed is True
    assert result.judge is None and result.judge_eligible is True


# ── Provenance on every result (GWT-3) ────────────────────────────────────────


def test_gwt3_eval_results_carry_a_resolvable_calibration_id_and_an_interval():
    cid = record_judge_calibration(_cal(judge="prov-judge", kappa=0.66, position_bias=0.04))
    record_eval_result(
        "s",
        "JPCP",
        {"judge_score": 0.8},
        run_id="r1",
        model_version="7",
        sample_size=50,
        judge={"model": "prov-judge", "prompt_version": "v1"},
    )
    row = next(r for r in get_eval_results("JPCP", "s") if r["metric"] == "judge_score")
    assert row["calibration_id"] == cid

    resolved = get_calibration_by_id(row["calibration_id"])
    assert resolved is not None
    assert resolved["kappa"] == pytest.approx(0.66)
    assert resolved["position_bias"] == pytest.approx(0.04)

    # G7.4 — the score is an interval, not a point value.
    assert row["score_lo"] < row["score"] < row["score_hi"]


def test_a_re_measurement_does_not_rewrite_the_provenance_of_an_old_result():
    old_id = record_judge_calibration(_cal(judge="drifting", kappa=0.7))
    record_eval_result(
        "s", "M", {"judge_score": 0.8}, run_id="r1", sample_size=10, judge={"model": "drifting"}
    )
    new_id = record_judge_calibration(
        _cal(judge="drifting", kappa=0.2, at="2026-09-01T00:00:00+00:00")
    )
    assert new_id != old_id
    row = next(r for r in get_eval_results("M", "s"))
    assert row["calibration_id"] == old_id
    assert get_calibration_by_id(old_id)["kappa"] == pytest.approx(0.7)


def test_a_non_proportion_metric_gets_no_fabricated_interval():
    record_eval_result("s", "M", {"rmse": 4.2}, run_id="r1", sample_size=10)
    row = next(r for r in get_eval_results("M", "s") if r["metric"] == "rmse")
    assert row["score_lo"] is None and row["score_hi"] is None


def test_a_unit_bearing_metric_inside_zero_to_one_also_gets_no_interval():
    """The gap `rmse` did not cover: a non-proportion that happens to be small.

    `rmse = 4.2` is excluded by the range test alone. A latency of 0.01 s and a cost of $0.0225
    are not — both land inside [0, 1] and were given a Wilson interval, which claimed a p50
    latency of 0.01 s might really be 0.45 s. Wilson is defined for k successes out of n trials
    and says nothing about a duration or a price.
    """
    record_eval_result(
        "s",
        "M",
        {"latency_p50": 0.01, "cost_usd": 0.0225},
        run_id="r1",
        sample_size=5,
        non_proportion_metrics={"latency_p50", "cost_usd"},
    )
    rows = {r["metric"]: r for r in get_eval_results("M", "s")}
    for metric in ("latency_p50", "cost_usd"):
        assert rows[metric]["score_lo"] is None, f"{metric} kept a fabricated interval"
        assert rows[metric]["score_hi"] is None


def test_declaring_units_does_not_take_the_interval_off_a_real_proportion():
    """`usage_reported_rate` is a genuine k/n and must keep its interval — the declaration is a
    list of units, not a blanket opt-out for the run."""
    record_eval_result(
        "s",
        "M",
        {"usage_reported_rate": 1.0, "tokens_per_answer": 1200.0},
        run_id="r1",
        sample_size=5,
        non_proportion_metrics={"tokens_per_answer"},
    )
    rows = {r["metric"]: r for r in get_eval_results("M", "s")}
    assert rows["usage_reported_rate"]["score_lo"] is not None
    assert rows["tokens_per_answer"]["score_lo"] is None


def test_an_undeclared_metric_keeps_the_previous_behaviour():
    """The change fails closed: callers that declare nothing — `run_suite` and every evaluator
    metric it persists — are unaffected."""
    record_eval_result("s", "M", {"accuracy": 0.8}, run_id="r1", sample_size=10)
    row = next(r for r in get_eval_results("M", "s") if r["metric"] == "accuracy")
    assert row["score_lo"] is not None and row["score_hi"] is not None


def test_the_suites_declare_every_unit_metric_they_record():
    """The guard against this recurring: a new unit-bearing score added to `_latency_scores` or
    `usage_scores` must join the declaration, or it silently acquires an interval again the first
    time it happens to be small."""
    from examlops.cli.commands.eval_cmd import _NON_PROPORTION_METRICS, _latency_scores
    from examlops.evaluation.usage import Usage, usage_scores

    # Derived by *running* the two producers, not by restating their keys here — a list copied
    # into the test would keep passing after a new score was added to the helper.
    produced = set(_latency_scores([0.5, 1.5])) | set(
        usage_scores([Usage(1000, 1000, 2000)], model="gpt-4o")
    )
    # Everything they emit carries a unit except this one, which is a genuine k/n.
    proportions = {"usage_reported_rate"}
    undeclared = (produced - proportions) - _NON_PROPORTION_METRICS
    assert not undeclared, f"undeclared unit metrics: {sorted(undeclared)}"
    assert proportions <= produced, "the known-proportion list names a score nothing produces"

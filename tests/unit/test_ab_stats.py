from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.analysis.ab_stats import (  # noqa: E402
    analyze_ab,
    two_proportion_z_test,
    welch_t_test,
)


def test_welch_detects_clear_difference():
    a = [10.0, 10.1, 9.9, 10.2, 9.8, 10.0]
    b = [12.0, 12.1, 11.9, 12.2, 11.8, 12.0]
    r = welch_t_test(a, b)
    assert r["p_value"] < 0.001
    assert r["mean_a"] < r["mean_b"]


def test_welch_no_difference_for_similar_samples():
    a = [10.0, 10.1, 9.9, 10.2, 9.8]
    b = [10.05, 9.95, 10.1, 9.9, 10.0]
    r = welch_t_test(a, b)
    assert r["p_value"] > 0.05


def test_welch_identical_zero_variance():
    r = welch_t_test([5.0, 5.0, 5.0], [5.0, 5.0, 5.0])
    assert r["p_value"] == 1.0
    assert r["t_stat"] == 0.0


def test_welch_requires_min_two():
    with pytest.raises(ValueError):
        welch_t_test([1.0], [2.0, 3.0])


def test_two_proportion_z_detects_difference():
    # 90/100 vs 60/100 successes — clearly different rates
    r = two_proportion_z_test(90, 100, 60, 100)
    assert r["p_value"] < 0.001
    assert r["rate_a"] > r["rate_b"]


def test_two_proportion_z_equal_rates():
    r = two_proportion_z_test(50, 100, 50, 100)
    assert r["p_value"] == pytest.approx(1.0)


def test_analyze_ab_insufficient_sample():
    r = analyze_ab([1.0, 2.0], [1.0, 2.0], min_sample=30)
    assert r["verdict"] == "insufficient_sample"
    assert r["winner"] is None


def test_analyze_ab_higher_is_better_winner():
    a = [0.9 + (i % 3) * 0.01 for i in range(40)]
    b = [0.8 + (i % 3) * 0.01 for i in range(40)]
    r = analyze_ab(a, b, lower_is_better=False, min_sample=30)
    assert r["verdict"] == "significant"
    assert r["winner"] == "a"  # a has higher mean and higher-is-better


def test_analyze_ab_lower_is_better_flips_winner():
    # a has lower mean (better for RMSE/latency)
    a = [1.0 + (i % 3) * 0.01 for i in range(40)]
    b = [2.0 + (i % 3) * 0.01 for i in range(40)]
    r = analyze_ab(a, b, lower_is_better=True, min_sample=30)
    assert r["significant"] is True
    assert r["winner"] == "a"

    # same data but higher-is-better → b wins
    r2 = analyze_ab(a, b, lower_is_better=False, min_sample=30)
    assert r2["winner"] == "b"


def test_analyze_ab_no_difference():
    a = [1.0, 1.1, 0.9, 1.0, 1.05] * 8
    b = [1.02, 0.98, 1.0, 1.03, 0.97] * 8
    r = analyze_ab(a, b, min_sample=30)
    assert r["verdict"] == "no_difference"
    assert r["winner"] is None

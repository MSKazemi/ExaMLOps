# tests/unit/test_fairness_engine.py
"""ADR 0025 clause 2 — Fairlearn's MetricFrame computes the slice metrics, and changes nothing.

Fairlearn is the engine the ADR names; the pure-Python path is the fallback when it is not
installed. The two must agree exactly, or switching an install would move a fairness gate.
Also pins two bugs the per-row pairing fixed: accuracy divided by every prediction (so labels that
had not arrived yet counted as misses), and predictions zipped against another row's label.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import fairness  # noqa: E402

_FIELDS = ("n", "accuracy", "error", "selection_rate", "tpr", "fpr", "below_min")


def _groups(seed: int) -> dict[str, list[tuple[float, float | None]]]:
    rng = random.Random(seed)
    groups: dict[str, list[tuple[float, float | None]]] = {}
    for name in ("a", "b", "c", "d"):  # binary slices, some rows not labelled yet
        groups[name] = [
            (float(rng.randint(0, 1)), None if rng.random() < 0.3 else float(rng.randint(0, 1)))
            for _ in range(rng.randint(5, 60))
        ]
    groups["no-positives"] = [(float(rng.randint(0, 1)), 0.0) for _ in range(12)]
    groups["no-negatives"] = [(float(rng.randint(0, 1)), 1.0) for _ in range(12)]
    groups["regression"] = [(rng.uniform(0, 3), rng.uniform(0, 3)) for _ in range(20)]
    groups["unlabelled"] = [(float(rng.randint(0, 1)), None) for _ in range(9)]
    return groups


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_fairlearn_and_the_fallback_agree_exactly(seed):
    pytest.importorskip("fairlearn")
    groups = _groups(seed)
    engine = fairness._fairlearn_slice_metrics(groups, min_samples=30)
    fallback = [fairness._slice_metric(v, rows, 30) for v, rows in sorted(groups.items())]
    assert engine is not None and len(engine) == len(fallback)
    for got, want in zip(engine, fallback):
        assert got.slice_value == want.slice_value
        for f in _FIELDS:
            g, w = getattr(got, f), getattr(want, f)
            if w is None or isinstance(w, bool):
                assert g == w, (got.slice_value, f, g, w)
            else:
                assert g == pytest.approx(w, abs=1e-12), (got.slice_value, f, g, w)


def test_label_metrics_divide_by_the_labelled_rows_only():
    # 10 predictions, 5 labels, all 5 correct. The old denominator (all 10) reported 0.5.
    rows = [(1.0, 1.0)] * 5 + [(1.0, None)] * 5
    m = fairness._slice_metric("s", rows, 1)
    assert m.accuracy == 1.0
    assert m.selection_rate == 1.0  # needs no label: every prediction counts
    reg = fairness._slice_metric("r", [(2.0, 1.0), (3.0, None)], 1)
    assert reg.error == 1.0  # MAE over the one labelled row, not halved


def test_a_prediction_is_scored_against_its_own_label(tmp_path, monkeypatch):
    # Row 1 has no label yet; row 2's label is 0 and its prediction 0. Zipping two independent
    # lists paired row 1's prediction (1) with row 2's label (0) — a miss that never happened.
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    from examlops import platform_db

    platform_db.init_db()
    platform_db.record_fairness_sample("M", "region", "eu", prediction=1.0, label=None)
    platform_db.record_fairness_sample("M", "region", "eu", prediction=0.0, label=0.0)
    res = fairness.slice_metrics("M", "region", min_samples=1)
    assert res.slices[0].accuracy == 1.0


def test_the_engine_is_recorded_and_the_fallback_gives_the_same_numbers(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    from examlops import platform_db

    platform_db.init_db()
    rng = random.Random(9)
    for i in range(80):
        platform_db.record_fairness_sample(
            "M",
            "site",
            "north" if i % 2 else "south",
            prediction=float(rng.randint(0, 1)),
            label=float(rng.randint(0, 1)),
        )
    pytest.importorskip("fairlearn")
    with_engine = fairness.slice_metrics("M", "site")
    assert with_engine.engine == "fairlearn" and with_engine.as_dict()["engine"] == "fairlearn"
    monkeypatch.setitem(sys.modules, "fairlearn", None)  # simulate a install without it
    monkeypatch.setitem(sys.modules, "fairlearn.metrics", None)
    fallback = fairness.slice_metrics("M", "site")
    assert fallback.engine == "pure-python"
    assert fallback.demographic_parity_diff == pytest.approx(with_engine.demographic_parity_diff)
    assert fallback.equalized_odds_diff == pytest.approx(with_engine.equalized_odds_diff)


def test_a_label_without_a_prediction_is_not_scored(tmp_path, monkeypatch):
    # Ground truth can land for a request whose prediction was never recorded. It pairs with
    # nothing, so it must neither crash scoring nor count as a sample.
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    from examlops import platform_db

    platform_db.init_db()
    platform_db.record_fairness_sample("M", "region", "eu", prediction=1.0, label=1.0)
    platform_db.record_fairness_sample("M", "region", "eu", prediction=None, label=0.0)
    res = fairness.slice_metrics("M", "region", min_samples=1)
    assert res.slices[0].n == 1 and res.slices[0].accuracy == 1.0

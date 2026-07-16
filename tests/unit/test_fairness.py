"""C8 — fairness & subgroup performance monitoring (ADR 0025).

GWT acceptance criteria from ``design/vision/specs/C8-fairness-subgroup-monitoring.md`` §5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _seed(model, attr, value, preds, labels, tenant="default"):
    from examlops import platform_db

    for p, y in zip(preds, labels):
        platform_db.record_fairness_sample(model, attr, value, tenant=tenant, prediction=p, label=y)


def test_gwt1_per_slice_metrics():
    """GWT-1: each declared slice's performance is reported."""
    from examlops import platform_db
    from examlops.fairness import slice_metrics

    platform_db.set_fairness_config("JPCP", ["region"], min_samples=5)
    _seed("JPCP", "region", "north", [1.0] * 8 + [0.0] * 2, [1.0] * 8 + [0.0] * 2)  # perfect
    _seed("JPCP", "region", "south", [1.0] * 5 + [0.0] * 5, [0.0] * 5 + [1.0] * 5)  # all wrong

    res = slice_metrics("JPCP", "region")
    by_val = {s.slice_value: s for s in res.slices}
    assert by_val["north"].n == 10
    assert by_val["north"].accuracy == pytest.approx(1.0)
    assert by_val["south"].accuracy == pytest.approx(0.0)


def test_gwt2_disparity_recorded():
    """GWT-2: a model under-performing on one slice yields nonzero DP/accuracy disparity."""
    from examlops import platform_db
    from examlops.fairness import slice_metrics

    platform_db.set_fairness_config("JPCP", ["region"], min_samples=5, threshold=0.1)
    # north selected 80%, south selected 20% => demographic-parity diff = 0.6
    _seed("JPCP", "region", "north", [1.0] * 8 + [0.0] * 2, [1.0] * 8 + [0.0] * 2)
    _seed("JPCP", "region", "south", [1.0] * 2 + [0.0] * 8, [1.0] * 2 + [0.0] * 8)

    res = slice_metrics("JPCP", "region")
    assert res.demographic_parity_diff == pytest.approx(0.6, abs=0.01)
    assert res.disparity_exceeded is True


def test_gwt3_noise_guard_excludes_small_slices():
    """GWT-3: a slice with < min samples is excluded from disparity/alerting."""
    from examlops import platform_db
    from examlops.fairness import slice_metrics

    platform_db.set_fairness_config("JPCP", ["region"], min_samples=30, threshold=0.1)
    # Big fair slice + a tiny extreme slice that would blow disparity if counted.
    _seed("JPCP", "region", "north", [1.0] * 50, [1.0] * 50)
    _seed("JPCP", "region", "tiny", [0.0] * 3, [1.0] * 3)  # below min => ignored

    res = slice_metrics("JPCP", "region")
    tiny = next(s for s in res.slices if s.slice_value == "tiny")
    assert tiny.below_min is True
    # Only one eligible slice => no disparity computed.
    assert res.disparity_exceeded is False


def test_gwt4_fairness_gate():
    """GWT-4: disparity above threshold + gate enabled => fairness_gate True."""
    from examlops import platform_db
    from examlops.fairness import fairness_gate

    platform_db.set_fairness_config(
        "JPCP", ["region"], min_samples=5, threshold=0.1, gate_promotion=True
    )
    _seed("JPCP", "region", "north", [1.0] * 8 + [0.0] * 2, [1.0] * 8 + [0.0] * 2)
    _seed("JPCP", "region", "south", [1.0] * 2 + [0.0] * 8, [1.0] * 2 + [0.0] * 8)
    assert fairness_gate("JPCP") is True


def test_fairness_gate_off_when_not_flagged():
    from examlops import platform_db
    from examlops.fairness import fairness_gate

    platform_db.set_fairness_config(
        "JPCP", ["region"], min_samples=5, threshold=0.1, gate_promotion=False
    )
    _seed("JPCP", "region", "north", [1.0] * 8, [1.0] * 8)
    _seed("JPCP", "region", "south", [0.0] * 8, [1.0] * 8)
    assert fairness_gate("JPCP") is False  # gate not enabled


def test_equalized_odds_diff():
    from examlops import platform_db
    from examlops.fairness import slice_metrics

    platform_db.set_fairness_config("JPCP", ["grp"], min_samples=4)
    # grp A: TPR high; grp B: TPR low => equalized-odds diff > 0
    _seed("JPCP", "grp", "A", [1.0, 1.0, 1.0, 0.0], [1.0, 1.0, 1.0, 0.0])
    _seed("JPCP", "grp", "B", [0.0, 0.0, 1.0, 0.0], [1.0, 1.0, 1.0, 0.0])
    res = slice_metrics("JPCP", "grp")
    assert res.equalized_odds_diff is not None
    assert res.equalized_odds_diff > 0


def test_regression_slice_error():
    """Non-binary predictions yield MAE per slice, not accuracy."""
    from examlops import platform_db
    from examlops.fairness import slice_metrics

    platform_db.set_fairness_config("REG", ["region"], min_samples=3)
    _seed("REG", "region", "north", [1.5, 2.5, 3.5], [1.0, 2.0, 3.0])  # MAE 0.5
    res = slice_metrics("REG", "region")
    north = next(s for s in res.slices if s.slice_value == "north")
    assert north.error == pytest.approx(0.5)
    assert north.accuracy is None


def test_report_all_attrs():
    from examlops import platform_db
    from examlops.fairness import fairness_report

    platform_db.set_fairness_config("JPCP", ["region", "tier"], min_samples=3)
    _seed("JPCP", "region", "north", [1.0] * 5, [1.0] * 5)
    _seed("JPCP", "tier", "gold", [1.0] * 5, [1.0] * 5)
    results = fairness_report("JPCP")
    assert {r.slice_attr for r in results} == {"region", "tier"}


def test_tenant_scoped():
    from examlops import platform_db
    from examlops.fairness import slice_metrics

    platform_db.set_fairness_config("JPCP", ["region"], min_samples=2)
    _seed("JPCP", "region", "north", [1.0, 1.0], [1.0, 1.0], tenant="acme")
    res_acme = slice_metrics("JPCP", "region", tenant="acme")
    res_other = slice_metrics("JPCP", "region", tenant="globex")
    assert len(res_acme.slices) == 1
    assert len(res_other.slices) == 0


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(
        app, ["fairness", "config", "JPCP", "--attr", "region", "--threshold", "0.1"]
    )
    assert r1.exit_code == 0, r1.output
    _seed("JPCP", "region", "north", [1.0] * 8 + [0.0] * 2, [1.0] * 8 + [0.0] * 2)
    _seed("JPCP", "region", "south", [1.0] * 2 + [0.0] * 8, [1.0] * 2 + [0.0] * 8)
    r2 = runner.invoke(app, ["fairness", "slice", "JPCP", "region"])
    assert r2.exit_code == 0, r2.output
    assert "north" in r2.output
    r3 = runner.invoke(app, ["fairness", "report", "JPCP"])
    assert r3.exit_code == 0, r3.output

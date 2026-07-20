"""C5 — advanced drift: concept / label-free perf / data-quality (ADR 0022).

GWT acceptance criteria from ``design/vision/specs/C5-concept-drift.md`` §5.
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


def _seed_pairs(model, preds, labels, alias="Production"):
    """Insert prediction+ground_truth pairs sharing a request_hash."""
    from examlops import platform_db

    with platform_db.get_db() as conn:
        for i, (p, lbl) in enumerate(zip(preds, labels)):
            h = f"{model}-{i}"
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction) VALUES (?,?,?,?)",
                (model, alias, h, p),
            )
            conn.execute(
                "INSERT INTO ground_truth (request_hash, label) VALUES (?,?)",
                (h, lbl),
            )


def test_gwt1_concept_drift_detected():
    """GWT-1: a changed input→target relationship records a concept drift."""
    from examlops import platform_db
    from examlops.drift_advanced import detect_concept_drift

    # Baseline: near-perfect predictions (low error). Recent: large errors.
    preds = [1.0] * 60 + [1.0] * 30
    labels = [1.0] * 60 + [5.0] * 30  # relationship broke in the recent tail
    _seed_pairs("JPCP", preds, labels)

    res = detect_concept_drift("JPCP", window=30)
    assert res.drift_kind == "concept"
    assert res.severity in ("WARN", "CRITICAL")
    assert res.score > 2.0

    events = platform_db.list_drift_events(model="JPCP", drift_kind="concept")
    assert events and events[0]["severity"] == res.severity


def test_concept_stable_is_ok():
    from examlops.drift_advanced import detect_concept_drift

    preds = [1.0] * 90
    labels = [1.0] * 90  # perfectly stable
    _seed_pairs("Stable", preds, labels)
    res = detect_concept_drift("Stable", window=30)
    assert res.severity == "OK"


def test_concept_insufficient_labels():
    from examlops.drift_advanced import detect_concept_drift

    _seed_pairs("Sparse", [1.0, 2.0], [1.0, 2.0])
    res = detect_concept_drift("Sparse", window=50)
    assert res.severity == "OK"
    assert res.detail.get("reason") == "insufficient labels"


def test_gwt2_concept_critical_triggers_autoretrain(monkeypatch):
    """GWT-2: a concept-CRITICAL detection past cooldown makes `drift trigger` fire."""
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    # Enable auto-retrain and record a concept-CRITICAL event, no prediction snapshots.
    platform_db.set_drift_auto_retrain("JPCP", enabled=True, dataset_name="PM100Dataset")
    platform_db.record_drift_event("JPCP", "concept", severity="CRITICAL", score=4.2)

    runner = CliRunner()
    result = runner.invoke(app, ["drift", "trigger", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output


def test_gwt3_label_free_estimate_warns():
    """GWT-3: a seeded degradation makes the label-free estimate drop and warn."""
    from examlops import platform_db
    from examlops.drift_advanced import estimate_performance

    # Low-confidence probabilistic predictions (near 0.5) => low estimated accuracy.
    with platform_db.get_db() as conn:
        for i in range(50):
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction) VALUES (?,?,?,?)",
                ("Clf", "Production", f"h{i}", 0.52),
            )
    res = estimate_performance("Clf", baseline=0.95)
    assert res["estimated"] is not None
    assert res["estimated"] < 0.7
    assert res["warn"] is True
    assert res["method"] == "cbpe-like"


def test_estimate_no_predictions():
    from examlops.drift_advanced import estimate_performance

    res = estimate_performance("Nothing")
    assert res["estimated"] is None
    assert res["n"] == 0


def test_gwt4_data_quality_null_spike():
    """GWT-4: inputs with a null spike record a data_quality drift."""
    from examlops import platform_db
    from examlops.drift_advanced import profile_inference

    batch = [{"a": None, "b": 1.0} for _ in range(10)]  # column a is all-null
    prof = profile_inference("JPCP", batch)
    assert prof.severity in ("WARN", "CRITICAL")
    assert prof.null_fraction > 0.2
    assert prof.fields["a"]["null_fraction"] == 1.0

    events = platform_db.list_drift_events(model="JPCP", drift_kind="data_quality")
    assert events and events[0]["drift_kind"] == "data_quality"


def test_data_quality_clean_ok():
    from examlops.drift_advanced import profile_inference

    batch = [{"a": float(i), "b": "x"} for i in range(20)]
    prof = profile_inference("Clean", batch)
    assert prof.severity == "OK"
    assert prof.fields["a"]["min"] == 0.0
    assert prof.fields["a"]["max"] == 19.0
    assert prof.fields["b"]["cardinality"] == 1


def test_data_quality_folds_bad_payloads():
    """A5 bad-payload counters escalate severity even with clean cells."""
    from examlops.drift_advanced import profile_inference

    batch = [{"a": 1.0} for _ in range(10)]
    prof = profile_inference("JPCP", batch, bad_payloads=40)  # 40/(10+40)=0.8
    assert prof.severity == "CRITICAL"


def test_gwt5_unified_events_by_kind():
    """GWT-5: every kind is queryable by drift_kind."""
    from examlops import platform_db

    for kind in ("feature", "prediction", "input_embedding", "concept", "data_quality"):
        platform_db.record_drift_event("M", kind, severity="OK", score=0.1)

    for kind in ("feature", "prediction", "input_embedding", "concept", "data_quality"):
        evs = platform_db.list_drift_events(model="M", drift_kind=kind)
        assert len(evs) == 1
        assert evs[0]["drift_kind"] == kind

    all_evs = platform_db.list_drift_events(model="M")
    assert len(all_evs) == 5


def test_perf_estimate_persisted():
    from examlops import platform_db

    platform_db.record_perf_estimate("M", "accuracy", estimated=0.9, realized=0.88, baseline=0.95)
    rows = platform_db.list_perf_estimates("M")
    assert len(rows) == 1
    assert rows[0]["estimated"] == 0.9
    assert rows[0]["realized"] == 0.88


def test_cli_events_smoke():
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    platform_db.record_drift_event("JPCP", "concept", severity="WARN", score=2.5)
    runner = CliRunner()
    result = runner.invoke(app, ["drift", "events"])
    assert result.exit_code == 0, result.output
    assert "concept" in result.output


def test_cli_concept_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    preds = [1.0] * 60 + [1.0] * 30
    labels = [1.0] * 60 + [9.0] * 30
    _seed_pairs("JPCP", preds, labels)
    runner = CliRunner()
    result = runner.invoke(app, ["drift", "concept", "JPCP", "--window", "30"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output

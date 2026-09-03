"""ADR 0114 — detect corruption before diagnosing drift.

The ADR's own verification protocol, run as tests:

1. Inject a nullification-pattern corruption into a served model's outputs.
2. Assert ``suspected_sdc``, ``classify_anomaly → suspected_hardware``, auto-retrain
   suppressed, an operator event raised, and the suppression recorded with its reason.
3. **Revert the guard → the retrain fires again.** The bug must return; a guard that
   cannot be shown to be load-bearing has not been shown to work.
4. Separately: real data drift (inputs *and* outputs move) → ``data_drift`` → retrain
   fires normally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops import corruption
from examlops.cli.main import app
from examlops.data.drift import (
    get_corruption_baseline,
    list_drift_events,
    set_corruption_baseline,
    set_drift_baseline,
    set_input_baseline,
    write_drift_snapshot,
    write_input_snapshot,
)
from examlops.platform_db import init_db

runner = CliRunner()

CLEAN = [10.0 + (i % 7) * 0.1 for i in range(200)]


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    yield
    del os.environ["PLATFORM_DB"]


# ── the statistic ─────────────────────────────────────────────────────────────


def test_nan_inf_alone_is_not_corruption_checked():
    """Decision 1: a NaN/Inf guard sees ~1% of the phenomenon, so the signal reports the
    zero rate too — and says so when it has no baseline to judge it against."""
    sig = corruption.detect_corruption(CLEAN)
    assert sig.nan_inf_rate == 0.0
    assert sig.zero_rate == 0.0
    assert sig.suspected_sdc is False
    assert "no corruption baseline" in sig.evidence
    assert any("no corruption baseline" in r for r in sig.reasons)


def test_special_values_are_detected():
    sig = corruption.detect_corruption(corruption.inject_special_values(CLEAN, 0.05, seed=1))
    assert sig.nan_inf_rate > 0
    assert sig.suspected_sdc is True


def test_nullification_is_detected_against_a_baseline():
    baseline = corruption.corruption_stats(CLEAN)
    corrupted = corruption.inject_nullification(CLEAN, 0.30, seed=2)
    sig = corruption.detect_corruption(corrupted, baseline)
    assert sig.suspected_sdc is True
    assert sig.zero_rate > 0.2
    assert sig.zero_rate_baseline == 0.0
    assert any("nullification" in r for r in sig.reasons)


def test_nullification_without_a_baseline_is_not_asserted():
    """Absent beats inferred (P5): with nothing to compare against, a zero rate is not
    evidence — the same numbers that fire above must not fire here."""
    corrupted = corruption.inject_nullification(CLEAN, 0.30, seed=2)
    assert corruption.detect_corruption(corrupted, None).suspected_sdc is False


def test_clean_signal_does_not_fire():
    baseline = corruption.corruption_stats(CLEAN)
    assert corruption.detect_corruption(CLEAN, baseline).suspected_sdc is False


def test_a_sparse_but_stable_model_does_not_fire():
    """Zero rates differ legitimately between models; the baseline is what makes a zero
    rate mean something, so a model that is *always* 30% zeros is not corrupt."""
    sparse = corruption.inject_nullification(CLEAN, 0.30, seed=3)
    baseline = corruption.corruption_stats(sparse)
    later = corruption.inject_nullification(CLEAN, 0.30, seed=4)
    assert corruption.detect_corruption(later, baseline).suspected_sdc is False


def test_detection_rate_is_measured_not_assumed():
    """R-ef: coverage is bounded by what was injected. The two gating classes must be
    caught; the untested one must be reported as not caught rather than credited."""
    baseline = corruption.corruption_stats(CLEAN)
    measured = corruption.measure_detection_rate(CLEAN, baseline, rate=0.30, trials=10)
    assert measured["nullification"]["detection_rate"] == 1.0
    assert measured["special_values"]["detection_rate"] == 1.0
    assert measured["mantissa_flip"]["gating"] is False
    assert corruption.DETECTOR_COVERAGE["mantissa_flip"]["sets_suspected_sdc"] is False
    assert measured["_clean"]["false_positive"] is False


# ── the classifier ────────────────────────────────────────────────────────────

_BREACH = {"z_score": 4.2, "status": "CRITICAL"}
_QUIET_INPUTS = {"max_z": 0.4, "status": "OK", "n_snapshots": 200}
_MOVING_INPUTS = {"max_z": 3.9, "status": "CRITICAL", "n_snapshots": 200}
_CLEAN_SIGNAL = corruption.detect_corruption(CLEAN, corruption.corruption_stats(CLEAN))


def test_corruption_outranks_a_drift_like_signal():
    """Decision 4: suppression is unconditional — even when the inputs moved too, which
    is the picture that otherwise reads as textbook data drift."""
    corrupt = corruption.detect_corruption(
        corruption.inject_nullification(CLEAN, 0.30, seed=5), corruption.corruption_stats(CLEAN)
    )
    cls = corruption.classify_anomaly(_BREACH, corrupt, _MOVING_INPUTS)
    assert cls.klass == "suspected_hardware"
    assert cls.remediation == "quarantine_node"
    assert cls.autonomous_remediation_allowed is False
    assert cls.operator_event is True


def test_both_axes_moving_is_data_drift():
    cls = corruption.classify_anomaly(_BREACH, _CLEAN_SIGNAL, _MOVING_INPUTS)
    assert cls.klass == "data_drift"
    assert cls.remediation == "retrain"
    assert cls.autonomous_remediation_allowed is True


def test_outputs_moved_but_inputs_held_is_a_regression():
    """Decision 5: prediction drift without input drift is evidence *against* data drift.
    Retraining would be the wrong remediation, so it is not permitted."""
    cls = corruption.classify_anomaly(_BREACH, _CLEAN_SIGNAL, _QUIET_INPUTS)
    assert cls.klass == "suspected_regression"
    assert cls.remediation == "rollback_deployment"
    assert cls.autonomous_remediation_allowed is False


def test_no_input_evidence_is_undetermined_not_data_drift():
    """Decision 3: when the signals do not separate, say so. The failure mode this whole
    ADR exists for is a confident answer from one axis."""
    cls = corruption.classify_anomaly(_BREACH, _CLEAN_SIGNAL, None)
    assert cls.klass == "undetermined"
    assert cls.autonomous_remediation_allowed is False
    assert cls.operator_event is True


def test_unset_input_baseline_is_undetermined():
    blind = {"max_z": 0.0, "status": "OK (no baseline)", "n_snapshots": 200}
    cls = corruption.classify_anomaly(_BREACH, _CLEAN_SIGNAL, blind)
    assert cls.klass == "undetermined"


def test_a_quiet_model_raises_no_operator_event():
    cls = corruption.classify_anomaly(
        {"z_score": 0.3, "status": "OK"}, _CLEAN_SIGNAL, _QUIET_INPUTS
    )
    assert cls.klass == "undetermined"
    assert cls.operator_event is False


def test_evidence_is_recorded_as_statistical_only():
    """Decision 6: with no hardware counters, downstream trust has to be told so."""
    assert corruption.classify_anomaly(_BREACH, _CLEAN_SIGNAL, _MOVING_INPUTS).evidence.startswith(
        "statistical_only"
    )


# ── the ADR's verification protocol, end to end ───────────────────────────────


def _seed_model(model: str, preds: list[float], *, input_z: float) -> None:
    """Populate both axes for `model`: predictions, an input baseline, and input snapshots
    placed `input_z` standard deviations away from that baseline."""
    for p in preds:
        write_drift_snapshot(model, "Production", p, None)
    # a baseline far from the live mean, so prediction drift genuinely breaches
    set_drift_baseline(model, {"mean": 5.0, "std": 0.2, "n": 200.0})
    set_corruption_baseline(model, corruption.corruption_stats(CLEAN))
    set_input_baseline(
        model,
        {
            "norm_mean": 1.0,
            "norm_mean_std": 0.1,
            "mean_mean": 0.0,
            "mean_mean_std": 0.1,
            "std_mean": 1.0,
            "std_mean_std": 0.1,
        },
    )
    offset = input_z * 0.1
    for _ in range(50):
        write_input_snapshot(model, "Production", 1.0 + offset, 0.0, 1.0, None)


def _enable_auto_retrain(model: str) -> None:
    result = runner.invoke(
        app, ["drift", "auto-retrain", "enable", model, "--dataset", "PM100Dataset"]
    )
    assert result.exit_code == 0, result.output


@pytest.fixture
def fake_post():
    with patch("examlops.cli._client.post") as mock:
        mock.return_value = {"flow_run_id": "run-123"}
        yield mock


def test_corrupted_model_is_suppressed_and_recorded(fake_post):
    """Steps 1–2 of the ADR's verification."""
    corrupted = corruption.inject_nullification(CLEAN, 0.30, seed=6)
    _seed_model("SDCMODEL", corrupted, input_z=4.0)
    _enable_auto_retrain("SDCMODEL")

    result = runner.invoke(app, ["--json", "drift", "trigger"])
    assert result.exit_code == 0, result.output
    assert fake_post.call_count == 0, "a retrain fired on a corruption signal"

    assert "suspected_hardware" in result.output
    events = list_drift_events(model="SDCMODEL", drift_kind="corruption")
    assert events, "no operator event was raised for the suppression"
    assert events[0]["severity"] == "CRITICAL"
    assert events[0]["detail"]["class"] == "suspected_hardware"
    assert events[0]["detail"]["reason"], "the suppression was recorded without its reason"


def test_reverting_the_guard_makes_the_retrain_fire_again(fake_post):
    """Step 3 — the bug must return. Without this the suppression above could be an
    accident of the fixture rather than the guard doing work."""
    corrupted = corruption.inject_nullification(CLEAN, 0.30, seed=6)
    _seed_model("SDCMODEL", corrupted, input_z=4.0)
    _enable_auto_retrain("SDCMODEL")

    # Revert the guard: classification returns nothing, so the path falls back to the
    # pre-ADR-0114 gates — a z-score threshold and a cooldown.
    with patch("examlops.cli.commands.drift._classify_or_none", return_value=None):
        result = runner.invoke(app, ["--json", "drift", "trigger"])
    assert result.exit_code == 0, result.output
    assert fake_post.call_count == 1, "the pre-guard bug did not return — the test proves nothing"


def test_real_data_drift_still_retrains(fake_post):
    """Step 4 — the guard must not be a blanket off-switch for the autopilot's reason to
    exist: inputs and outputs both moved, corruption is negative, so the retrain fires."""
    _seed_model("DRIFTMODEL", CLEAN, input_z=4.0)
    _enable_auto_retrain("DRIFTMODEL")

    result = runner.invoke(app, ["--json", "drift", "trigger"])
    assert result.exit_code == 0, result.output
    assert fake_post.call_count == 1, result.output


def test_quiet_inputs_suppress_as_a_suspected_regression(fake_post):
    """The remediation for a serving regression is a rollback, and retraining would paper
    over it — so the autonomous path must decline it."""
    _seed_model("REGMODEL", CLEAN, input_z=0.2)
    _enable_auto_retrain("REGMODEL")

    result = runner.invoke(app, ["--json", "drift", "trigger"])
    assert result.exit_code == 0, result.output
    assert fake_post.call_count == 0
    assert "suspected_regression" in result.output


def test_no_input_snapshots_means_no_autonomous_retrain(fake_post):
    """The behaviour change operators will notice first: prediction drift alone no longer
    fires a retrain, because one axis cannot tell data drift from a regression."""
    for p in CLEAN:
        write_drift_snapshot("LONEMODEL", "Production", p, None)
    set_drift_baseline("LONEMODEL", {"mean": 5.0, "std": 0.2, "n": 200.0})
    _enable_auto_retrain("LONEMODEL")

    result = runner.invoke(app, ["--json", "drift", "trigger"])
    assert result.exit_code == 0, result.output
    assert fake_post.call_count == 0
    assert "undetermined" in result.output


# ── the CLI surface ───────────────────────────────────────────────────────────


def test_corruption_baseline_and_status_round_trip():
    for p in CLEAN:
        write_drift_snapshot("BASEMODEL", "Production", p, None)
    result = runner.invoke(app, ["drift", "corruption", "baseline", "BASEMODEL"])
    assert result.exit_code == 0, result.output
    stored = get_corruption_baseline("BASEMODEL")
    assert stored is not None and stored["zero_rate"] == 0.0

    result = runner.invoke(app, ["--json", "drift", "corruption", "status", "BASEMODEL"])
    assert result.exit_code == 0, result.output
    assert '"suspected_sdc": false' in result.output.lower()


def test_corruption_baseline_refuses_without_snapshots():
    result = runner.invoke(app, ["drift", "corruption", "baseline", "GHOST"])
    assert result.exit_code == 1, result.output


def test_classify_command_names_the_class():
    _seed_model("CLSMODEL", CLEAN, input_z=0.2)
    result = runner.invoke(app, ["--json", "drift", "corruption", "classify", "CLSMODEL"])
    assert result.exit_code == 0, result.output
    assert "suspected_regression" in result.output


def test_selftest_publishes_the_measured_rate():
    for p in CLEAN:
        write_drift_snapshot("STMODEL", "Production", p, None)
    set_corruption_baseline("STMODEL", corruption.corruption_stats(CLEAN))
    result = runner.invoke(
        app, ["--json", "drift", "corruption", "selftest", "STMODEL", "--trials", "5"]
    )
    assert result.exit_code == 0, result.output
    assert "detection_rate" in result.output
    assert "mantissa_flip" in result.output


def test_selftest_refuses_without_a_baseline():
    for p in CLEAN:
        write_drift_snapshot("NOBASE", "Production", p, None)
    result = runner.invoke(app, ["drift", "corruption", "selftest", "NOBASE"])
    assert result.exit_code == 1, result.output


def test_input_drift_rows_has_one_implementation():
    """The classifier and `exa drift input status` must read the same statistic; a second
    copy of it in the CLI is a second thing to keep in step."""
    from examlops.cli.commands import drift as drift_cmd

    _seed_model("SHAREDMODEL", CLEAN, input_z=4.0)
    assert drift_cmd._input_drift_rows("SHAREDMODEL") == corruption.input_drift_rows(
        "SHAREDMODEL", window=drift_cmd._INPUT_WINDOW
    )

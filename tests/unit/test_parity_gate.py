"""ADR 0117 — the quantisation arm of the portability gate.

Every gate this platform had was *single-target*: it validated a model on the backend it was
already running on. A portability gate is inherently two-target, and nothing did that — while
`quantize_model()` registered, signed, BOM'd and audited a requantised version with **no numeric
comparison at all**. Quantisation is a *deliberate* numeric change, so a promotion that changed
the numerics shipped on a green latency check.

The ADR's verification protocol, run as tests:

1. A promotion that does **not** change the execution target skips the gate entirely.
2. A change of quantisation runs parity; divergence beyond tolerance **blocks** and records the
   measured divergence.
3. With no second target reachable, the gate reports **`inert`** — never `passed`.
3b. **The vacuous-pass trap.** On a host without CUDA `quantize_model()` records provenance only
    and the weights are unchanged. A comparator that simply diffed outputs would report perfect
    parity and green-light the promotion, having measured nothing. Red-green: the gate must not
    report `passed` when the artefact is unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops import parity
from examlops.cli.main import app
from examlops.data import get_db
from examlops.data.audit import write_audit_event
from examlops.platform_db import init_db

runner = CliRunner()

BASE = [1.0, 2.0, 3.0, 4.0]


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    yield
    del os.environ["PLATFORM_DB"]


def _record_quantization(model: str, version: str, *, transformed: bool) -> None:
    write_audit_event(
        "exa-engines",
        "tester",
        "model_quantized",
        f"{model}@{version}",
        {
            "base_version": version.rsplit("-", 1)[0],
            "method": "awq",
            "weights_transformed": transformed,
            "provenance_only": not transformed,
        },
    )


# ── the comparator ────────────────────────────────────────────────────────────


def test_divergence_of_identical_vectors_is_zero():
    assert parity.divergence(BASE, list(BASE)) == (0.0, 0.0)


def test_divergence_is_relative_to_the_source_magnitude():
    max_abs, max_rel = parity.divergence([100.0], [101.0])
    assert max_abs == pytest.approx(1.0)
    assert max_rel == pytest.approx(0.01)


def test_a_zero_source_falls_back_to_absolute():
    """A change from 0 to 0.5 is not a 0% change — dividing by zero here would hide it."""
    _, max_rel = parity.divergence([0.0], [0.5])
    assert max_rel == pytest.approx(0.5)


def test_a_non_finite_output_is_a_divergence_not_something_to_skip():
    assert parity.divergence([1.0], [float("nan")]) == (float("inf"), float("inf"))
    assert parity.divergence([1.0], [float("inf")]) == (float("inf"), float("inf"))


def test_mismatched_lengths_are_infinite_divergence():
    assert parity.divergence([1.0, 2.0], [1.0]) == (float("inf"), float("inf"))


# ── the verdicts ──────────────────────────────────────────────────────────────


def test_an_untransformed_artefact_is_inert_not_passed():
    """Verification 3b, the vacuous-pass trap: identical outputs prove nothing when nothing
    was transformed, and this is the assertion that stops the gate rubber-stamping."""
    res = parity.parity_gate(BASE, list(BASE), tolerance=1e-3, transformed=False)
    assert res.verdict == parity.INERT
    assert res.verdict != parity.PASSED
    assert res.permits_autonomous_promotion is False
    assert "not transformed" in res.reason


def test_identical_outputs_pass_only_when_a_transformation_occurred():
    res = parity.parity_gate(BASE, list(BASE), tolerance=1e-3, transformed=True)
    assert res.verdict == parity.PASSED
    assert res.permits_autonomous_promotion is True


def test_divergence_beyond_tolerance_blocks_and_records_the_measured_value():
    """Verification 2 — the measured divergence is recorded, not just pass/fail, so drift
    toward the boundary is visible before it crosses."""
    res = parity.parity_gate(BASE, [1.0, 2.0, 3.0, 4.4], tolerance=1e-3, transformed=True)
    assert res.verdict == parity.BLOCKED
    assert res.permits_autonomous_promotion is False
    assert res.max_abs_divergence == pytest.approx(0.4)
    assert res.max_rel_divergence == pytest.approx(0.1)
    assert "0.1" in res.reason


def test_divergence_within_tolerance_passes():
    res = parity.parity_gate(BASE, [1.0, 2.0, 3.0, 4.0004], tolerance=1e-3, transformed=True)
    assert res.verdict == parity.PASSED


def test_missing_fixtures_are_inert_not_passed():
    """Verification 3 — nothing to compare against is not a pass."""
    res = parity.parity_gate(None, None, tolerance=1e-3, transformed=True)
    assert res.verdict == parity.INERT
    assert "nothing was compared" in res.reason


def test_only_a_real_measured_pass_permits_autonomy():
    for verdict in (parity.INERT, parity.BLOCKED):
        res = parity.ParityResult(verdict=verdict, reason="")
        assert res.permits_autonomous_promotion is False


# ── target-change detection ───────────────────────────────────────────────────


def test_an_unchanged_target_is_detected():
    """Verification 1 — the gate is conditional on a target change, not a tax on every
    promotion."""
    assert parity.target_change_for_version("17") is None
    assert parity.target_change_for_version("3") is None


def test_a_quantised_version_names_its_method():
    assert parity.target_change_for_version("17-awq") == "awq"
    assert parity.target_change_for_version("3-fp8") == "fp8"


def test_the_gate_does_not_apply_to_an_unchanged_target():
    assert parity.run_quantization_parity_gate("JPCP", "17") is None


# ── provenance ────────────────────────────────────────────────────────────────


def test_quantize_model_records_whether_weights_changed():
    """The flag the gate depends on. Before ADR 0117 the audit event recorded only base_version
    and method, so the gate could not have told a real requantisation from a no-op."""
    import warnings as _warnings

    from examlops import engines

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", RuntimeWarning)
        engines.quantize_model("JPCP", "17", "awq", actor="tester")
    prov = parity.quantization_provenance("JPCP", "17-awq")
    assert prov is not None
    assert "weights_transformed" in prov
    # Nothing in quantize_model() invokes a quantizer on *either* path, so the honest value is
    # False regardless of this host's GPU. Deriving it from GPU presence would assert weights
    # changed where they did not, and the gate would then compare a model against itself and
    # report `passed` — the vacuous-pass trap one level up.
    assert prov["weights_transformed"] is False
    assert prov["provenance_only"] is True


def test_the_gate_is_inert_on_a_version_this_platform_quantized(tmp_path):
    """End-to-end on the real path: quantize, then gate. It must not report `passed`, because
    no quantizer ran — on this host or any other."""
    import warnings as _warnings

    from examlops import engines

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", RuntimeWarning)
        new_version = engines.quantize_model("JPCP", "31", "fp8", actor="tester")
    res = parity.run_quantization_parity_gate("JPCP", new_version, record=False)
    assert res is not None
    assert res.verdict == parity.INERT
    assert res.verdict != parity.PASSED
    assert res.permits_autonomous_promotion is False


def test_absent_provenance_is_treated_as_untransformed():
    """A version with no recorded quantisation is not evidence that weights changed — absent
    provenance must yield `inert`, never a free pass."""
    res = parity.run_quantization_parity_gate("GHOST", "9-awq", record=False)
    assert res is not None and res.verdict == parity.INERT
    assert res.transformed is False


def test_a_real_transformation_with_a_runner_is_measured():
    _record_quantization("JPCP", "17-awq", transformed=True)
    outputs = {"17": BASE, "17-awq": [1.0, 2.0, 3.0, 4.0004]}
    res = parity.run_quantization_parity_gate(
        "JPCP", "17-awq", runner=lambda m, v: outputs[v], tolerance=1e-3, record=False
    )
    assert res is not None and res.verdict == parity.PASSED
    assert res.n_fixtures == 4


def test_a_real_transformation_that_diverges_blocks():
    _record_quantization("JPCP", "17-awq", transformed=True)
    outputs = {"17": BASE, "17-awq": [1.0, 2.0, 3.0, 9.0]}
    res = parity.run_quantization_parity_gate(
        "JPCP", "17-awq", runner=lambda m, v: outputs[v], tolerance=1e-3, record=False
    )
    assert res is not None and res.verdict == parity.BLOCKED


def test_an_unreachable_target_leaves_the_gate_inert():
    """A runner that cannot reach a target must not be read as agreement."""
    _record_quantization("JPCP", "17-awq", transformed=True)

    def _boom(model, version):
        raise RuntimeError("engine unreachable")

    res = parity.run_quantization_parity_gate("JPCP", "17-awq", runner=_boom, record=False)
    assert res is not None and res.verdict == parity.INERT
    assert "could not run fixtures" in res.reason


# ── tolerance ─────────────────────────────────────────────────────────────────


def test_tolerance_is_read_per_model_from_the_pack(tmp_path, monkeypatch):
    """Decision 4 — declared per model and reviewed like any other gate threshold, because a
    ranking model tolerates far more drift than one whose output is a physical quantity."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "jpcp.yaml").write_text("name: JPCP\nparity_tolerance: 0.05\n")
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))
    assert parity.model_tolerance("JPCP") == pytest.approx(0.05)


def test_an_undeclared_tolerance_falls_back_to_a_tight_default(monkeypatch, tmp_path):
    monkeypatch.setenv("RAY_MODELS_DIR", str(tmp_path))
    assert parity.model_tolerance("NOSUCH") == parity.DEFAULT_TOLERANCE


# ── recording ─────────────────────────────────────────────────────────────────


def test_the_check_is_recorded_with_its_measured_divergence():
    _record_quantization("JPCP", "17-awq", transformed=True)
    outputs = {"17": BASE, "17-awq": [1.0, 2.0, 3.0, 9.0]}
    parity.run_quantization_parity_gate(
        "JPCP", "17-awq", runner=lambda m, v: outputs[v], tolerance=1e-3, actor="tester"
    )
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM parity_checks").fetchall()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action='parity_gate_evaluated'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == parity.BLOCKED
    assert rows[0]["max_rel_divergence"] == pytest.approx(1.25)
    assert rows[0]["transformed"] == 1
    assert audit, "the gate left no trace in the evidence chain"


# ── the CLI surface ───────────────────────────────────────────────────────────


def test_parity_command_reports_inert_for_an_untransformed_artefact():
    _record_quantization("JPCP", "17-awq", transformed=False)
    result = runner.invoke(app, ["--json", "models", "parity", "JPCP", "17-awq"])
    assert result.exit_code == 0, result.output
    assert '"verdict": "inert"' in result.output
    assert '"verdict": "passed"' not in result.output


def test_parity_command_says_the_gate_does_not_apply_to_an_unchanged_target():
    result = runner.invoke(app, ["models", "parity", "JPCP", "17"])
    assert result.exit_code == 0, result.output
    assert "does not change the execution target" in result.output

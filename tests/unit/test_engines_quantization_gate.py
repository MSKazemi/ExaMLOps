"""ADR 0016 decision 3 — the mandatory C3 quality-retention gate for a quantized version.

The gate compares ``<base>-<method>`` against ``<base>`` on the model's C3 suite, refuses when no
gate is configured or either side is unscored, always blocks (even for a ``warn``-mode gate), and
is enforced on the promotion paths: ``promotion_refusal`` (training flow) and
``exa models quantize-gate`` (CI step). Every verdict lands in ``gate_reports`` and the audit log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.main import app  # noqa: E402
from examlops.data.evaluation import (  # noqa: E402
    get_gate_reports,
    record_eval_result,
    set_eval_gate,
)
from examlops.engines.quality import quantization_quality_gate  # noqa: E402
from examlops.evaluation.gate import promotion_refusal  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "unit-test")
    monkeypatch.delenv("EXAMLOPS_QUANTIZATION_MAX_DROP", raising=False)
    init_db()


def _gate(mode: str = "block", metrics=None) -> None:
    set_eval_gate("JPCP", "qa", metrics or [{"name": "accuracy"}], mode=mode)


def _scores(version: str, accuracy: float, run: str | None = None) -> None:
    record_eval_result(
        "qa", "JPCP", {"accuracy": accuracy}, run_id=run or f"run-{version}", model_version=version
    )


def _audit(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT target, details FROM audit_events WHERE action=? ORDER BY id", (action,)
        ).fetchall()
    return [{"target": r["target"], **json.loads(r["details"] or "{}")} for r in rows]


# ── applicability ─────────────────────────────────────────────────────────────


def test_an_unquantized_version_is_not_this_gates_business():
    assert quantization_quality_gate("JPCP", "17") is None
    assert get_gate_reports("JPCP") == []


# ── refusals: absent is never a pass ──────────────────────────────────────────


def test_a_lost_quantization_gate_audit_is_counted_and_the_verdict_stands(monkeypatch, caplog):
    """Recording is best-effort; the refusal is not. Both losses are visible, neither is silent."""
    import examlops.data.evaluation as evaluation
    from examlops.data import audit

    audit.reset_dropped_audit_events()
    monkeypatch.setattr(audit, "write_audit_event", lambda *a, **k: (_ for _ in ()).throw(OSError))
    monkeypatch.setattr(
        evaluation, "record_gate_report", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    try:
        with caplog.at_level("WARNING", logger="examlops.engines.quality"):
            res = quantization_quality_gate("JPCP", "17-awq")
        assert res is not None and res.passed is False  # the verdict survives both losses
        assert audit.dropped_audit_events().get("quantization_quality_gate") == 1
        assert any("not recorded" in r.getMessage() for r in caplog.records)
    finally:
        audit.reset_dropped_audit_events()


def test_no_configured_gate_refuses():
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res is not None and res.passed is False
    assert "no C3 eval gate" in res.reason
    assert (res.base_version, res.method) == ("17", "awq")
    (report,) = get_gate_reports("JPCP")
    assert report["passed"] == 0 and report["candidate"] == "17-awq"
    (event,) = _audit("quantization_quality_gate")
    assert event["target"] == "JPCP@17-awq" and event["passed"] is False


def test_unscored_base_refuses():
    _gate()
    _scores("17-awq", 0.9)
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res.passed is False and "base version 17 has no scores" in res.reason


def test_unscored_candidate_refuses():
    _gate()
    _scores("17", 0.9)
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res.passed is False and res.failing == ["accuracy"]


# ── retention is measured against the base version ────────────────────────────


def test_small_drop_within_default_passes():
    _gate()
    _scores("17", 0.900)
    _scores("17-awq", 0.895)
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res.passed is True, res.reason
    assert res.report["metrics"][0]["baseline"] == 0.9  # the base, not a baseline alias


def test_drop_beyond_default_blocks():
    _gate()
    _scores("17", 0.90)
    _scores("17-awq", 0.85)
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res.passed is False and res.failing == ["accuracy"]
    (report,) = get_gate_reports("JPCP")
    assert report["baseline"] == "version:17"


def test_a_warn_mode_gate_still_blocks_a_quantization():
    _gate(mode="warn")
    _scores("17", 0.90)
    _scores("17-awq", 0.50)
    assert quantization_quality_gate("JPCP", "17-awq").passed is False


def test_a_metrics_own_max_drop_wins_over_the_default():
    _gate(metrics=[{"name": "accuracy", "max_drop": 0.1}])
    _scores("17", 0.90)
    _scores("17-awq", 0.85)
    assert quantization_quality_gate("JPCP", "17-awq").passed is True


def test_default_drop_is_configurable_and_fails_closed(monkeypatch):
    _gate()
    _scores("17", 0.90)
    _scores("17-awq", 0.85)
    monkeypatch.setenv("EXAMLOPS_QUANTIZATION_MAX_DROP", "0.2")
    assert quantization_quality_gate("JPCP", "17-awq").passed is True
    monkeypatch.setenv("EXAMLOPS_QUANTIZATION_MAX_DROP", "-5")  # nonsense ⇒ default 0.01
    assert quantization_quality_gate("JPCP", "17-awq").passed is False
    monkeypatch.setenv("EXAMLOPS_QUANTIZATION_MAX_DROP", "lots")
    assert quantization_quality_gate("JPCP", "17-awq").passed is False


def test_a_gate_metric_the_base_never_scored_refuses():
    """Base scored on accuracy only; f1 would otherwise pass unmeasured (no baseline ⇒ no drop)."""
    _gate(metrics=[{"name": "accuracy"}, {"name": "f1"}])
    _scores("17", 0.90)
    record_eval_result(
        "qa", "JPCP", {"accuracy": 0.90, "f1": 0.10}, run_id="q", model_version="17-awq"
    )
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res.passed is False
    assert res.failing == ["f1"] and "no score for f1" in res.reason


def test_a_majority_gate_cannot_outvote_a_quantization_loss():
    """One of three metrics lost quality: `majority` would pass it for a retrain, not here."""
    set_eval_gate(
        "JPCP",
        "qa",
        [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        mode="block",
        aggregate="majority",
    )
    record_eval_result("qa", "JPCP", {"a": 0.9, "b": 0.9, "c": 0.9}, run_id="b", model_version="17")
    record_eval_result(
        "qa", "JPCP", {"a": 0.9, "b": 0.9, "c": 0.5}, run_id="q", model_version="17-awq"
    )
    res = quantization_quality_gate("JPCP", "17-awq")
    assert res.passed is False and res.failing == ["c"]


def test_the_newest_score_wins_a_same_second_tie():
    _gate()
    _scores("17", 0.90)
    _scores("17-awq", 0.50, run="first")
    _scores("17-awq", 0.90, run="rerun")  # same second, written later
    assert quantization_quality_gate("JPCP", "17-awq").passed is True


# ── enforcement on the promotion paths ────────────────────────────────────────


def test_training_flow_uses_the_name_the_gate_is_configured_under():
    """`names` = [registry name, MLflow name]; the gate lives under the second one."""
    set_eval_gate("jpcp", "qa", [{"name": "accuracy"}], mode="block")
    record_eval_result("qa", "jpcp", {"accuracy": 0.9}, run_id="b", model_version="17")
    record_eval_result("qa", "jpcp", {"accuracy": 0.9}, run_id="q", model_version="17-awq")
    record_eval_result("qa", "jpcp", {"accuracy": 0.9}, run_id="prod", alias="Production")
    assert promotion_refusal(["JPCP", "jpcp"], "17-awq") is None


def test_training_flow_promotion_refuses_an_ungated_quantization():
    reason = promotion_refusal(["JPCP"], "17-awq", actor="pipeline")
    assert reason is not None and "no C3 eval gate" in reason
    (event,) = _audit("promotion_blocked_by_quantization_gate")
    assert event["version"] == "17-awq"


def test_training_flow_promotion_unchanged_for_a_normal_version():
    assert promotion_refusal(["JPCP"], "17") is None


def test_training_flow_promotion_allows_a_retained_quantization():
    _gate()
    _scores("17", 0.90)
    _scores("17-awq", 0.90)
    # The general C3 gate compares against the Production alias; give it scores too.
    record_eval_result("qa", "JPCP", {"accuracy": 0.9}, run_id="prod", alias="Production")
    assert promotion_refusal(["JPCP"], "17-awq") is None


def _promote_quantized(*flags):
    """`exa pipeline promote` with MLflow faked to report version 17-awq under Staging."""
    from unittest.mock import patch

    def _get(url, **_):
        if "registered-models/get" in url:
            return {"registered_model": {"aliases": [{"alias": "Staging", "version": "17-awq"}]}}
        if "model-versions/get" in url:
            return {"model_version": {"run_id": "r", "version": "17-awq"}}
        if "runs/get" in url:
            return {"run": {"data": {"metrics": {"rmse": 1.0}}}}
        return {}

    args = ["--yes", "pipeline", "promote", "JPCP", "--if-rmse-lt", "5.0", *flags]
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, args)
    return res, post


def test_exa_pipeline_promote_refuses_an_ungated_quantization():
    res, post = _promote_quantized()
    assert "Quantization quality gate FAILED" in res.output
    assert post.call_count == 0  # the alias never moved
    (event,) = _audit("promotion_blocked_by_quantization_gate")
    assert event["version"] == "17-awq"


def test_exa_pipeline_promote_force_overrides_and_audits():
    res, _post = _promote_quantized("--force")
    assert "overriding" in res.output
    (event,) = _audit("quantization_gate_override")
    assert event["forced"] is True


def test_cli_quantize_gate_exit_codes_and_json():
    refused = runner.invoke(app, ["--json", "models", "quantize-gate", "JPCP", "17-awq"])
    assert refused.exit_code == 1
    assert json.loads(refused.output)["passed"] is False

    _gate()
    _scores("17", 0.90)
    _scores("17-awq", 0.90)
    ok = runner.invoke(app, ["models", "quantize-gate", "JPCP", "17-awq"])
    assert ok.exit_code == 0, ok.output
    assert "quality retained" in ok.output

    na = runner.invoke(app, ["models", "quantize-gate", "JPCP", "17"])
    assert na.exit_code == 0 and "not a quantized version" in na.output

# tests/unit/test_eval_gate_aggregate_and_validate.py
"""ADR 0008 clauses 2 and 5 — the gate in `validate-model`, and the aggregate decision.

Clause 5 says the aggregate "considers all configured metrics together, so a single noisy
metric cannot alone block a genuine improvement (subject to policy)". The gate blocked on
`any(v.failed …)`, which is exactly the case the clause set out to avoid. Clause 2 says
`exa pipeline validate-model` runs the eval gate alongside the latency check; it ran only the
latency check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.commands import pipeline as pipeline_cmd  # noqa: E402
from examlops.data.evaluation import get_eval_gate, set_eval_gate  # noqa: E402
from examlops.evaluation.gate import AGGREGATES, evaluate_gate  # noqa: E402

runner = CliRunner()

# Three metrics; `a` regresses hard, `b` and `c` improve.
_THREE = [
    {"name": "a", "max_drop": 0.01},
    {"name": "b", "max_drop": 0.01},
    {"name": "c", "max_drop": 0.01},
]
_BASE = {"a": 0.90, "b": 0.90, "c": 0.90}
_ONE_BAD = {"a": 0.50, "b": 0.95, "c": 0.96}


# ── clause 5: the aggregate ───────────────────────────────────────────────────


def test_the_default_is_unchanged_any_failure_still_blocks():
    """Changing the default would silently weaken every gate already configured."""
    assert evaluate_gate(_THREE, _ONE_BAD, _BASE).passed is False
    assert evaluate_gate(_THREE, _ONE_BAD, _BASE).aggregate == "all"


def test_majority_lets_one_noisy_regression_be_outvoted():
    result = evaluate_gate(_THREE, _ONE_BAD, _BASE, aggregate="majority")
    assert result.passed is True
    assert result.aggregate == "majority"
    # The failure is still *recorded* — outvoted is not the same as unnoticed.
    assert [m.name for m in result.metrics if m.failed] == ["a"]


def test_majority_still_blocks_when_the_failures_are_the_majority():
    two_bad = {"a": 0.50, "b": 0.40, "c": 0.96}
    assert evaluate_gate(_THREE, two_bad, _BASE, aggregate="majority").passed is False


def test_an_exact_half_does_not_carry_a_majority():
    two = [{"name": "a", "max_drop": 0.01}, {"name": "b", "max_drop": 0.01}]
    one_bad = {"a": 0.50, "b": 0.95}
    assert evaluate_gate(two, one_bad, _BASE, aggregate="majority").passed is True


def test_a_ceiling_violation_blocks_alone_under_majority():
    """A safety cap that unrelated metrics can outvote is not a cap."""
    cfg = [
        {"name": "unsafe_rate", "max": 0.01, "higher_is_better": False},
        {"name": "b", "max_drop": 0.01},
        {"name": "c", "max_drop": 0.01},
    ]
    scores = {"unsafe_rate": 0.90, "b": 0.95, "c": 0.96}
    assert evaluate_gate(cfg, scores, _BASE, aggregate="majority").passed is False


def test_a_floor_violation_blocks_alone_under_majority():
    cfg = [{"name": "a", "min": 0.80}, {"name": "b", "max_drop": 0.01}, {"name": "c"}]
    scores = {"a": 0.10, "b": 0.95, "c": 0.96}
    assert evaluate_gate(cfg, scores, _BASE, aggregate="majority").passed is False


def test_a_missing_candidate_score_blocks_alone_under_majority():
    """Nothing was measured; that is not noise to be outvoted."""
    result = evaluate_gate(_THREE, {"b": 0.95, "c": 0.96}, _BASE, aggregate="majority")
    assert result.passed is False


def test_absolute_failures_are_marked_hard_and_regressions_are_not():
    cfg = [{"name": "a", "min": 0.80, "max_drop": 0.01}, {"name": "b", "max_drop": 0.01}]
    verdicts = {m.name: m for m in evaluate_gate(cfg, {"a": 0.10, "b": 0.50}, _BASE).metrics}
    assert verdicts["a"].hard is True  # floor
    assert verdicts["b"].failed is True and verdicts["b"].hard is False  # regression only


def test_an_unknown_policy_fails_closed():
    """A typo in the config must not quietly loosen the gate."""
    result = evaluate_gate(_THREE, _ONE_BAD, _BASE, aggregate="majorityy")
    assert result.passed is False
    assert result.aggregate == "all"
    assert set(AGGREGATES) == {"all", "majority"}


def test_the_policy_is_recorded_on_the_report():
    """A pass under `majority` and a pass under `all` are different claims."""
    assert evaluate_gate(_THREE, _ONE_BAD, _BASE, aggregate="majority").as_dict()["aggregate"] == (
        "majority"
    )


def test_the_policy_round_trips_through_the_gate_config():
    set_eval_gate("agg-model", "quality", _THREE, aggregate="majority")
    assert get_eval_gate("agg-model")["aggregate"] == "majority"


def test_a_gate_stored_before_the_column_existed_reads_as_all():
    set_eval_gate("legacy-model", "quality", _THREE)
    gate = get_eval_gate("legacy-model")
    assert gate["aggregate"] is None
    assert evaluate_gate(_THREE, _ONE_BAD, _BASE, aggregate=gate["aggregate"]).passed is False


# ── clause 2: validate-model runs the gate ────────────────────────────────────


@pytest.fixture
def serving(monkeypatch):
    """A healthy Ray Serve, so only the eval gate can decide the outcome."""
    monkeypatch.setattr(pipeline_cmd._client, "post", lambda *a, **k: {"prediction": 1.0})
    monkeypatch.setattr(
        "examlops.cli.commands.rollback_cmd._get_current_alias_version", lambda *a, **k: 7
    )


@pytest.fixture
def json_out(monkeypatch):
    """`--json` lives on the root app; this sub-app is invoked directly, so set the mode."""
    from examlops.cli import _output

    monkeypatch.setattr(_output, "json_mode", True)
    monkeypatch.setattr(_output, "output_format", "json", raising=False)


def _run(*args):
    return runner.invoke(pipeline_cmd.app, ["validate-model", "JPCP", *args])


def test_a_failing_eval_gate_fails_validate_model(serving, monkeypatch):
    from examlops.evaluation.gate import GateResult, MetricVerdict

    failed = GateResult(
        passed=False,
        mode="block",
        metrics=[MetricVerdict("accuracy", 0.1, 0.9, -0.8, None, 0.01, True, "regressed")],
    )
    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: {"suite": "quality"})
    monkeypatch.setattr("examlops.evaluation.gate.run_eval_gate", lambda *a, **k: failed)

    result = _run()

    assert result.exit_code == 1
    assert "Eval gate FAILED" in result.output
    assert "accuracy" in result.output


def test_a_passing_eval_gate_leaves_validate_model_green(serving, monkeypatch):
    from examlops.evaluation.gate import GateResult

    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: {"suite": "quality"})
    monkeypatch.setattr(
        "examlops.evaluation.gate.run_eval_gate",
        lambda *a, **k: GateResult(passed=True, mode="block"),
    )
    assert _run().exit_code == 0


def test_no_configured_gate_is_reported_as_a_skip_not_a_pass(serving, json_out, monkeypatch):
    """A green latency check beside a silently-absent eval gate reads as 'validated'."""
    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: None)
    result = runner.invoke(pipeline_cmd.app, ["validate-model", "JPCP"])
    assert result.exit_code == 0
    assert '"eval_gate": "SKIP"' in result.output
    assert "no gate configured" in result.output


def test_an_unresolvable_alias_is_reported_with_its_reason(serving, json_out, monkeypatch):
    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: {"suite": "quality"})
    monkeypatch.setattr(
        "examlops.cli.commands.rollback_cmd._get_current_alias_version", lambda *a, **k: None
    )
    result = runner.invoke(pipeline_cmd.app, ["validate-model", "JPCP"])
    assert result.exit_code == 0
    assert "could not resolve" in result.output


def test_a_broken_gate_is_a_reported_skip_not_a_pass(serving, json_out, monkeypatch):
    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: {"suite": "quality"})

    def _boom(*_a, **_k):
        raise RuntimeError("gate table unreadable")

    monkeypatch.setattr("examlops.evaluation.gate.run_eval_gate", _boom)
    result = runner.invoke(pipeline_cmd.app, ["validate-model", "JPCP"])
    assert result.exit_code == 0
    assert "gate error" in result.output


def test_a_warn_mode_pass_is_distinguishable_from_nothing_failing(serving, json_out, monkeypatch):
    from examlops.evaluation.gate import GateResult, MetricVerdict

    warned = GateResult(
        passed=True,
        mode="warn",
        metrics=[MetricVerdict("accuracy", 0.1, 0.9, -0.8, None, 0.01, True, "regressed")],
    )
    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: {"suite": "quality"})
    monkeypatch.setattr("examlops.evaluation.gate.run_eval_gate", lambda *a, **k: warned)

    result = runner.invoke(pipeline_cmd.app, ["validate-model", "JPCP"])

    assert result.exit_code == 0
    assert "not blocking" in result.output
    assert "accuracy" in result.output


def test_latency_failure_still_wins_before_the_gate_is_consulted(serving, monkeypatch):
    """The gate must not turn a latency failure into a pass, whatever it says."""
    from examlops.evaluation.gate import GateResult

    monkeypatch.setattr(pipeline_cmd, "_lookup_eval_gate", lambda m: {"suite": "quality"})
    monkeypatch.setattr(
        "examlops.evaluation.gate.run_eval_gate",
        lambda *a, **k: GateResult(passed=True, mode="block"),
    )
    assert _run("--max-latency", "0.0").exit_code == 1

# tests/unit/test_prompt_label_gate.py
"""ADR 0009 clause 4 — a prompt label move gated by the C3 eval regression check.

The clause says a label move "can be **gated by the eval regression check (C3) exactly like
model promotion**". The registry, the audited move and the six CLI commands shipped; the gate
did not, so the one lifecycle control the clause names for its riskiest operation was absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.commands import prompt_cmd  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result, set_eval_gate  # noqa: E402
from examlops.data.prompts import (  # noqa: E402
    create_prompt_version,
    get_prompt_by_label,
    set_prompt_label,
)

runner = CliRunner()


@pytest.fixture
def prompt(monkeypatch):
    """A prompt with two versions and `prod` on v1."""
    monkeypatch.delenv("EXAMLOPS_PROMPT_GATE_LABELS", raising=False)
    create_prompt_version("triage", "Classify: {text}")
    create_prompt_version("triage", "Classify carefully: {text}")
    set_prompt_label("triage", "prod", 1)
    return "triage"


def _gate_on(subject: str = "prompt:triage") -> None:
    set_eval_gate(
        subject, "quality", [{"name": "accuracy", "max_drop": 0.01}], baseline_alias="prod"
    )


def _scores(subject: str, *, version: str | None = None, alias: str | None = None, score: float):
    record_eval_result(
        "quality",
        subject,
        {"accuracy": score},
        run_id=f"run-{subject}-{version or alias}",
        model_version=version,
        alias=alias,
    )


def _labels(name: str) -> int | None:
    row = get_prompt_by_label(name, "prod")
    return None if row is None else row["version"]


# ── the gate holds ────────────────────────────────────────────────────────────


def test_a_regressing_prompt_version_cannot_take_the_prod_label(prompt):
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.50)

    result = runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2"])

    assert result.exit_code == 1
    assert "Eval gate FAILED" in result.output
    assert _labels("triage") == 1, "the label must not have moved"


def test_the_refusal_is_audited_with_the_failing_metrics(prompt):
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.50)

    runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2"])

    events = [e for e in export_audit_events() if e["action"] == "prompt_label_blocked_by_gate"]
    assert events, "a blocked release that leaves no D4 record is not governed"
    assert "accuracy" in str(events[0]["details"])


def test_a_passing_version_moves_the_label(prompt):
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.95)

    result = runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2"])

    assert result.exit_code == 0
    assert _labels("triage") == 2


def test_force_overrides_and_is_audited(prompt):
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.50)

    result = runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2", "--force"])

    assert result.exit_code == 0
    assert _labels("triage") == 2
    assert [e for e in export_audit_events() if e["action"] == "eval_gate_override"]


# ── the gate stays out of the way ─────────────────────────────────────────────


def test_no_configured_gate_is_a_no_op(prompt):
    """Nobody who has not asked for a gate may notice one exists."""
    result = runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2"])
    assert result.exit_code == 0
    assert _labels("triage") == 2


def test_a_prompt_does_not_inherit_a_model_s_gate_of_the_same_name(prompt):
    """`eval_gates` is one keyspace shared with models; a bare name would cross the subjects."""
    set_eval_gate(
        "triage", "quality", [{"name": "accuracy", "max_drop": 0.01}], baseline_alias="prod"
    )
    _scores("triage", alias="prod", score=0.90)
    _scores("triage", version="2", score=0.10)  # the *model* triage regressed badly

    result = runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2"])

    assert result.exit_code == 0, "a model's scores must not decide a prompt's release"
    assert _labels("triage") == 2


def test_staging_and_dev_moves_are_not_gated(prompt):
    """Gating them deadlocks the registry: the gate's baseline must itself be a labelled version."""
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.10)

    for label_name in ("dev", "staging"):
        result = runner.invoke(prompt_cmd.app, ["label", "triage", label_name, "2"])
        assert result.exit_code == 0, f"{label_name} must stay ungated"


def test_the_gated_label_set_is_configurable(prompt, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PROMPT_GATE_LABELS", "staging, prod")
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.10)

    assert runner.invoke(prompt_cmd.app, ["label", "triage", "staging", "2"]).exit_code == 1


def test_rollback_is_never_gated(prompt):
    """A rollback is the remedy when scores are failing; gating it traps the operator."""
    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="1", score=0.10)
    set_prompt_label("triage", "prod", 2)

    result = runner.invoke(prompt_cmd.app, ["rollback", "triage", "prod", "1"])

    assert result.exit_code == 0
    assert _labels("triage") == 1


def test_a_broken_gate_does_not_strand_a_release(prompt, monkeypatch):
    """With a gate configured *and* failing, the only way through is the except branch."""
    import examlops.evaluation.gate as gate_mod

    _gate_on()
    _scores("prompt:triage", alias="prod", score=0.90)
    _scores("prompt:triage", version="2", score=0.10)

    def _boom(*_a, **_k):
        raise RuntimeError("gate table unreadable")

    monkeypatch.setattr(gate_mod, "run_eval_gate", _boom)
    assert runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "2"]).exit_code == 0
    assert _labels("triage") == 2


def test_a_nonexistent_version_is_rejected_before_the_gate_runs(prompt):
    result = runner.invoke(prompt_cmd.app, ["label", "triage", "prod", "99"])
    assert result.exit_code == 1
    assert "does not exist" in result.output

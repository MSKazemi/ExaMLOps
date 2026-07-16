# tests/unit/test_evaluation.py
"""C2 continuous eval (ADR 0007) + C3 regression gate (ADR 0008).

C2: GWT-1 deterministic · GWT-2 judge persisted · GWT-3 sampling · GWT-4 idempotency ·
GWT-5 calibration. C3: GWT-1 regression block · GWT-2 floor block · GWT-3 warn.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import evaluation as ev  # noqa: E402
from examlops.evaluation import gate as gate_mod  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    get_eval_results,
    init_db,
    set_eval_gate,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


# ── C2 ────────────────────────────────────────────────────────────────────────


def test_gwt1_exact_match_fraction():
    items = [
        ev.EvalItem(output="a", reference="a"),
        ev.EvalItem(output="b", reference="c"),
        ev.EvalItem(output="d", reference="d"),
    ]
    suite = ev.Suite("det", [ev.ExactMatch()])
    result = suite.run(items)
    assert result.scores["exact_match"] == pytest.approx(2 / 3)


def test_json_valid_and_regex_evaluators():
    suite = ev.Suite("det", [ev.JSONValid(), ev.Regex(r"\d+")])
    r = suite.run([ev.EvalItem(output='{"x": 1}'), ev.EvalItem(output="no-json-here-7")])
    assert r.scores["json_valid"] == 0.5
    assert r.scores["regex_match"] == 1.0


def test_gwt2_judge_scored_and_persisted():
    judge = ev.LLMJudge(
        judge_fn=lambda prompt: 0.9, rubric="grade", judge_model="gpt-judge", prompt_version="v3"
    )
    suite = ev.Suite("judged", [judge])
    ev.run_suite(
        suite,
        [ev.EvalItem(output="hello", reference="hi")],
        model="JPCP",
        run_id="r1",
        model_version="18",
    )
    rows = get_eval_results("JPCP", "judged")
    assert rows[0]["score"] == pytest.approx(0.9)
    assert rows[0]["judge_model"] == "gpt-judge"
    assert rows[0]["judge_prompt_version"] == "v3"


def test_judge_clamps_out_of_range():
    judge = ev.LLMJudge(judge_fn=lambda p: 2.5, rubric="r")
    s = judge.score(ev.EvalItem(output="x"))
    assert s.score == 1.0


def test_gwt3_sampling_by_request_hash():
    items = [ev.EvalItem(output=f"o{i}", prompt=f"p{i}") for i in range(100)]
    sampled = ev.sample_by_request_hash(items, 20)
    assert len(sampled) == 20
    # deterministic: same selection on repeat
    assert [it.output for it in sampled] == [
        it.output for it in ev.sample_by_request_hash(items, 20)
    ]


def test_gwt4_persistence_idempotent():
    suite = ev.Suite("s", [ev.ExactMatch()])
    for _ in range(2):
        ev.run_suite(
            suite,
            [ev.EvalItem(output="a", reference="a")],
            model="JPCP",
            run_id="same-run",
            model_version="18",
        )
    rows = get_eval_results("JPCP", "s")
    assert len(rows) == 1  # UNIQUE(suite, version, run, metric) → no dup


def test_gwt5_judge_calibration():
    cal = ev.judge_calibration([0.9, 0.2, 0.8], [1.0, 0.0, 0.6])
    assert cal["agreement"] == pytest.approx(1.0)
    assert cal["n"] == 3


# ── C3 ────────────────────────────────────────────────────────────────────────


def test_gwt1_regression_blocks():
    result = gate_mod.evaluate_gate(
        [{"name": "accuracy", "max_drop": 0.01}],
        candidate_scores={"accuracy": 0.90},
        baseline_scores={"accuracy": 0.93},
        mode="block",
    )
    assert result.passed is False
    assert result.metrics[0].failed is True


def test_gwt2_floor_blocks():
    result = gate_mod.evaluate_gate(
        [{"name": "groundedness", "min": 0.8}],
        candidate_scores={"groundedness": 0.75},
        baseline_scores={},
        mode="block",
    )
    assert result.passed is False


def test_gwt3_warn_mode_passes_but_records():
    result = gate_mod.evaluate_gate(
        [{"name": "accuracy", "max_drop": 0.01}],
        candidate_scores={"accuracy": 0.90},
        baseline_scores={"accuracy": 0.93},
        mode="warn",
    )
    assert result.passed is True  # warn never blocks
    assert result.metrics[0].failed is True  # but the regression is recorded


def test_gate_passes_when_within_tolerance():
    result = gate_mod.evaluate_gate(
        [{"name": "accuracy", "max_drop": 0.05, "min": 0.8}],
        candidate_scores={"accuracy": 0.91},
        baseline_scores={"accuracy": 0.93},
        mode="block",
    )
    assert result.passed is True


def test_lower_is_better_regression():
    # For an error metric (rmse), higher candidate = regression.
    result = gate_mod.evaluate_gate(
        [{"name": "rmse", "max_drop": 0.1}],
        candidate_scores={"rmse": 5.0},
        baseline_scores={"rmse": 4.5},
        mode="block",
        higher_is_better=False,
    )
    assert result.passed is False


def test_run_eval_gate_end_to_end():
    set_eval_gate("JPCP", "smoke", [{"name": "accuracy", "max_drop": 0.01}], mode="block")
    result = gate_mod.run_eval_gate(
        "JPCP",
        "18",
        candidate_scores={"accuracy": 0.90},
        baseline_scores={"accuracy": 0.95},
    )
    assert result is not None
    assert result.passed is False
    # report persisted
    from examlops.platform_db import get_gate_reports

    reports = get_gate_reports("JPCP")
    assert reports and reports[0]["passed"] == 0


def test_run_eval_gate_none_when_unconfigured():
    assert gate_mod.run_eval_gate("Unconfigured", "1", candidate_scores={}) is None

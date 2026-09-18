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


def test_a_gate_can_hold_metrics_that_point_in_opposite_directions():
    """The agent suites store a mix in one scores dict — `answer_rate` up, `unsafe_rate` down.

    With one direction for the whole gate there is no correct setting for such a config:
    `higher_is_better=True` lets `unsafe_rate` rise to 1.0 unnoticed, and `False` fails a
    *perfect* `answer_rate` of 1.0 against its own floor. A safety gate that cannot fail on
    unsafe behaviour reports PASS on the one run that most needed it to say otherwise.
    """
    cfg = [
        {"name": "answer_rate", "min": 0.95},
        {"name": "unsafe_rate", "min": 0.0, "higher_is_better": False},
    ]
    result = gate_mod.evaluate_gate(
        cfg,
        candidate_scores={"answer_rate": 1.0, "unsafe_rate": 1.0},
        baseline_scores={"answer_rate": 1.0, "unsafe_rate": 0.0},
        mode="block",
        higher_is_better=True,
    )
    by_name = {m.name: m for m in result.metrics}
    assert by_name["unsafe_rate"].failed is True, (
        "every mutating request executed and the gate passed"
    )
    assert by_name["answer_rate"].failed is False, (
        "a perfect answer_rate must not fail its own floor"
    )
    assert result.passed is False


def test_a_ceiling_is_a_ceiling_whichever_way_the_gate_leans():
    """`max` is direction-independent, so a latency budget survives the gate-level flag.

    Expressing "p95 must stay under 30 s" as a *floor* read backwards is how the direction bug
    got written in the first place; a ceiling that means the same thing under both settings
    cannot be mis-configured into silence.
    """
    for lean in (True, False):
        result = gate_mod.evaluate_gate(
            [{"name": "latency_p95", "max": 30.0}],
            candidate_scores={"latency_p95": 300.0},
            baseline_scores={"latency_p95": 22.0},
            mode="block",
            higher_is_better=lean,
        )
        assert result.passed is False, f"ceiling ignored with higher_is_better={lean}"
        assert "ceiling" in result.metrics[0].reason


def test_the_gate_spec_parser_accepts_a_direction_and_a_ceiling():
    """`exa eval gate set --metric …` is the only authoring surface, so a per-metric direction
    that the parser cannot express does not exist for an operator."""
    from examlops.cli.commands.eval_cmd import _parse_metric_spec

    assert _parse_metric_spec("unsafe_rate:max=0.05:higher_is_better=false") == {
        "name": "unsafe_rate",
        "max": 0.05,
        "higher_is_better": False,
    }
    assert _parse_metric_spec("accuracy:min=0.8:max_drop=0.01") == {
        "name": "accuracy",
        "min": 0.8,
        "max_drop": 0.01,
    }


def test_the_gate_owns_its_direction_rather_than_borrowing_the_callers():
    """Both promotion roads derive the gate direction from the promotion *rule's* operator.

    `exa pipeline promote jpcp --if-rmse-lt 5.0` and the autopilot both compute
    `higher_is_better = operator in ("gt","gte")` — from a threshold on one MLflow metric — and
    hand that to a gate whose config names entirely different suite metrics. A model gated on
    `rmse` therefore defaults every eval metric to lower-is-better, so an `accuracy` regression
    is read backwards by whoever happens to call the gate. Direction is a property of the gate,
    declared where the gate is authored; the caller's value is only a fallback.
    """
    set_eval_gate(
        "DirModel",
        "smoke",
        [{"name": "accuracy", "max_drop": 0.01}],
        mode="block",
        higher_is_better=True,
    )
    result = gate_mod.run_eval_gate(
        "DirModel",
        "2",
        candidate_scores={"accuracy": 0.80},
        baseline_scores={"accuracy": 0.95},
        higher_is_better=False,  # what an `--if-rmse-lt` caller passes
        persist=False,
    )
    assert result is not None
    assert result.passed is False, "the gate's own direction must beat the caller's"


def test_a_gate_that_declares_no_direction_still_takes_the_callers():
    """Every gate configured before this existed has no stored direction, and must behave
    exactly as it did — the fallback is what makes the column additive rather than a change."""
    set_eval_gate("OldModel", "smoke", [{"name": "rmse", "max_drop": 0.1}], mode="block")
    result = gate_mod.run_eval_gate(
        "OldModel",
        "2",
        candidate_scores={"rmse": 5.0},
        baseline_scores={"rmse": 4.5},
        higher_is_better=False,
        persist=False,
    )
    assert result is not None
    assert result.passed is False, "rmse rose; with the caller's lower-is-better that regresses"


def test_a_per_metric_direction_still_beats_the_gates_own():
    """Precedence is per-metric > gate > caller, so a mixed suite stays expressible.

    Pass 219 made one gate able to hold metrics that point both ways; a gate-level direction
    that overrode those would take it straight back.
    """
    set_eval_gate(
        "MixedModel",
        "agent-safety",
        [
            {"name": "answer_rate", "min": 0.95},
            {"name": "unsafe_rate", "min": 0.0, "higher_is_better": False},
        ],
        mode="block",
        higher_is_better=True,
    )
    result = gate_mod.run_eval_gate(
        "MixedModel",
        "2",
        candidate_scores={"answer_rate": 1.0, "unsafe_rate": 1.0},
        baseline_scores={"answer_rate": 1.0, "unsafe_rate": 0.0},
        persist=False,
    )
    assert result is not None
    by_name = {m.name: m for m in result.metrics}
    assert by_name["unsafe_rate"].failed is True
    assert by_name["answer_rate"].failed is False


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


def test_gate_reports_are_newest_first_even_within_one_second():
    """`reports[0]` is read as the standing verdict, so a tie on `ts` must not decide it.

    Two gate reports land in one second whenever a promote and an autopilot cycle judge the same
    model, or a CI matrix runs the suite twice. `ts` has one-second resolution, so ordering by it
    alone leaves the choice to the query plan — and SQLite returns the *oldest* of the tied rows,
    which here means the superseded verdict is handed to the MCP `gate_reports` tool as current.
    """
    from examlops import platform_db
    from examlops.platform_db import get_gate_reports

    with platform_db.get_db() as conn:
        for i, passed in enumerate((1, 0), start=1):  # the second one is the standing verdict
            conn.execute(
                "INSERT INTO gate_reports (ts, model, candidate, baseline, passed, mode, "
                "report_json) VALUES ('2026-09-15 12:00:00', 'TIED', ?, '1', ?, 'block', '{}')",
                (str(i + 1), passed),
            )

    reports = get_gate_reports("TIED")
    assert [r["candidate"] for r in reports] == ["3", "2"], reports
    assert reports[0]["passed"] == 0, "the newest verdict is the failing one"

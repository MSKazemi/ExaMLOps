# tests/unit/test_challenger_judge_scoring.py
"""ADR 0024 clause 2's other half — scoring a challenger with a C2 judge.

The clause reads "when labels arrive (ground-truth) **or via a C2 judge**". The ground-truth half
shipped; the judge half did not, and `_score_errors` skipped every sample whose label was None —
so a shadow deployment where ground truth never arrives produced an empty scoreboard forever,
which is precisely the case the judge exists for. The `challenger_samples.label` column even
carried the comment "filled as ground truth / C2 judge arrives", and nothing filled it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import champion_challenger as cc  # noqa: E402
from examlops.cli.commands import challenger_cmd  # noqa: E402
from examlops.data.serving import (  # noqa: E402
    get_challenger_samples,
    record_challenger_sample,
    set_challenger_config,
)

runner = CliRunner()


def _sample(model, *, champ, chall, label=None):
    record_challenger_sample(model, champion_pred=champ, challenger_pred=chall, label=label)


def _enable(model, **over):
    cfg = {"challenger_version": "2", "min_delta": 0.0, "alpha": 0.05, "min_samples": 2}
    cfg.update(over)
    set_challenger_config(
        model,
        challenger_version=cfg["challenger_version"],
        mirror_pct=100,
        min_delta=cfg["min_delta"],
        alpha=cfg["alpha"],
        min_samples=cfg["min_samples"],
        enabled=True,
        updated_by="test",
    )


# ── the scoring pass ──────────────────────────────────────────────────────────


def test_unlabelled_samples_get_judge_scores():
    _sample("JudgeA", champ=1.0, chall=2.0)
    _sample("JudgeA", champ=1.0, chall=2.0)

    out = cc.score_samples_with_judge("JudgeA", lambda c, g: (0.4, 0.9), judge_model="j1")

    assert out["scored"] == 2
    rows = get_challenger_samples("JudgeA", labelled_only=False)
    assert all(r["challenger_judge"] == 0.9 for r in rows)
    assert all(r["judge_model"] == "j1" for r in rows)


def test_a_labelled_sample_is_never_rescored_by_a_judge():
    """Ground truth wins; preferring a judge where a label exists replaces a measurement."""
    _sample("JudgeB", champ=1.0, chall=2.0, label=1.0)

    out = cc.score_samples_with_judge("JudgeB", lambda c, g: (0.1, 0.9), judge_model="j1")

    assert out["scored"] == 0
    assert get_challenger_samples("JudgeB", labelled_only=False)[0]["champion_judge"] is None


def test_judge_scores_are_not_written_into_the_label_column():
    """A judged sample indistinguishable from a measured one makes the scoreboard a mixture
    nobody can separate afterwards."""
    _sample("JudgeC", champ=1.0, chall=2.0)

    cc.score_samples_with_judge("JudgeC", lambda c, g: (0.4, 0.9), judge_model="j1")

    assert get_challenger_samples("JudgeC", labelled_only=False)[0]["label"] is None


def test_a_judge_error_leaves_the_sample_unscored():
    """A judge error is not a bad prediction; scoring it 0 would move the decision."""

    def _boom(_c, _g):
        raise RuntimeError("gateway down")

    _sample("JudgeD", champ=1.0, chall=2.0)

    out = cc.score_samples_with_judge("JudgeD", _boom, judge_model="j1")

    assert (out["scored"], out["failed"]) == (0, 1)
    assert get_challenger_samples("JudgeD", labelled_only=False)[0]["champion_judge"] is None


def test_scores_are_clamped_into_the_zero_one_range():
    _sample("JudgeE", champ=1.0, chall=2.0)
    cc.score_samples_with_judge("JudgeE", lambda c, g: (-3, 7), judge_model="j1")
    row = get_challenger_samples("JudgeE", labelled_only=False)[0]
    assert (row["champion_judge"], row["challenger_judge"]) == (0.0, 1.0)


def test_a_scoring_pass_is_audited():
    from examlops.data.audit import export_audit_events

    _sample("JudgeF", champ=1.0, chall=2.0)
    cc.score_samples_with_judge("JudgeF", lambda c, g: (0.5, 0.5), judge_model="j1")

    assert [e for e in export_audit_events() if e["action"] == "challenger_judge_scored"]


# ── the scoreboard ────────────────────────────────────────────────────────────


def test_a_judged_scoreboard_reports_judge_evidence(monkeypatch):
    monkeypatch.setattr(cc, "_judge_eligibility", lambda j: (True, []))
    _enable("BoardA")
    for _ in range(4):
        _sample("BoardA", champ=1.0, chall=2.0)
    cc.score_samples_with_judge("BoardA", lambda c, g: (0.2, 0.9), judge_model="j1")

    status = cc.challenger_status("BoardA")

    assert status.evidence == "judge"
    assert status.n == 4
    assert status.challenger_error < status.champion_error  # 1 - 0.9 < 1 - 0.2


def test_a_labelled_scoreboard_still_reports_labels(monkeypatch):
    monkeypatch.setattr(cc, "_judge_eligibility", lambda j: (True, []))
    _enable("BoardB")
    for _ in range(4):
        _sample("BoardB", champ=1.0, chall=1.5, label=1.5)

    assert cc.challenger_status("BoardB").evidence == "labels"


def test_a_mixed_scoreboard_says_so(monkeypatch):
    """A promotion decision resting on two kinds of evidence must be able to say so."""
    monkeypatch.setattr(cc, "_judge_eligibility", lambda j: (True, []))
    _enable("BoardC")
    _sample("BoardC", champ=1.0, chall=1.5, label=1.5)
    _sample("BoardC", champ=1.0, chall=2.0)
    cc.score_samples_with_judge("BoardC", lambda c, g: (0.2, 0.9), judge_model="j1")

    assert cc.challenger_status("BoardC").evidence == "mixed"


def test_an_empty_scoreboard_reports_no_evidence(monkeypatch):
    monkeypatch.setattr(cc, "_judge_eligibility", lambda j: (True, []))
    _enable("BoardD")
    _sample("BoardD", champ=1.0, chall=2.0)  # neither label nor judge

    status = cc.challenger_status("BoardD")

    assert status.evidence == "none"
    assert status.n == 0


def test_a_deployment_with_no_judge_scores_identically_to_before(monkeypatch):
    """Widening the sample query must not change a scoreboard that has no judge."""
    monkeypatch.setattr(cc, "_judge_eligibility", lambda j: (True, []))
    _enable("BoardE")
    for _ in range(3):
        _sample("BoardE", champ=1.0, chall=1.5, label=1.5)
    _sample("BoardE", champ=9.0, chall=9.0)  # unlabelled, unjudged — must be ignored

    status = cc.challenger_status("BoardE")

    assert status.n == 3
    assert status.champion_error == pytest.approx(0.5)


# ── ADR 0111 on this road too ─────────────────────────────────────────────────


def test_an_uncalibrated_judge_cannot_carry_a_promotion(monkeypatch):
    """A challenger promotion is the same decision `exa pipeline promote` makes another way."""
    monkeypatch.setattr(
        cc, "_judge_eligibility", lambda j: (False, ["no_calibration"]) if j else (True, [])
    )
    _enable("Cal1", min_samples=2)
    for _ in range(4):
        _sample("Cal1", champ=1.0, chall=2.0)
    cc.score_samples_with_judge("Cal1", lambda c, g: (0.1, 0.99), judge_model="uncal")

    status = cc.challenger_status("Cal1")

    assert status.judge == "uncal"
    assert status.judge_eligible is False
    assert status.policy_met is False, "an unmeasured instrument must not decide production"
    assert "no_calibration" in status.judge_failures


def test_a_ground_truth_scoreboard_is_unaffected_by_the_calibration_rule():
    """No judge decided it, so there is no instrument to calibrate."""
    _enable("Cal2", min_samples=2)
    for _ in range(4):
        _sample("Cal2", champ=2.0, chall=1.0, label=1.0)

    status = cc.challenger_status("Cal2")

    assert status.judge is None
    assert status.judge_eligible is True


def test_an_unanswerable_calibration_question_is_not_a_pass(monkeypatch):
    import examlops.evaluation.calibration as cal

    monkeypatch.setattr(
        cal, "is_gate_eligible", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    eligible, failures = cc._judge_eligibility("some-judge")
    assert eligible is False
    assert failures == ["judge_calibration_unavailable"]


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_the_cli_warns_when_the_judge_is_not_eligible(monkeypatch):
    monkeypatch.setattr(
        "examlops.evaluation.calibration.is_gate_eligible",
        lambda *a, **k: (False, ["no_calibration"]),
    )
    monkeypatch.setattr(
        "examlops.champion_challenger.score_samples_with_judge",
        lambda *a, **k: {"model": "M", "judge": "j", "scored": 0, "failed": 0},
    )

    result = runner.invoke(challenger_cmd.app, ["judge", "M"])

    assert result.exit_code == 0
    assert "not MVVP-eligible" in result.output
    assert "exa eval calibrate" in result.output


def test_the_cli_reports_a_clean_scoring_pass(monkeypatch):
    monkeypatch.setattr(
        "examlops.evaluation.calibration.is_gate_eligible", lambda *a, **k: (True, [])
    )
    monkeypatch.setattr(
        "examlops.champion_challenger.score_samples_with_judge",
        lambda *a, **k: {"model": "M", "judge": "j", "scored": 3, "failed": 1},
    )

    result = runner.invoke(challenger_cmd.app, ["judge", "M"])

    assert result.exit_code == 0
    assert "Judged 3" in result.output
    assert "1 skipped" in result.output

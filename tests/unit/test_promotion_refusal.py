# tests/unit/test_promotion_refusal.py
"""ADR 0008 — the training flow's own promotion must answer to the eval gate (BL-056).

`exa pipeline promote` and the autopilot run the gate before moving an alias; the training flow's
`promote_task` moved Staging/Canary/Production on metric thresholds alone, so a `block`-mode gate
stopped every road to Production but the one most versions take. The refusal decision now lives
with the gate (`examlops.evaluation.gate.promotion_refusal`), and this file holds it against a real
gate and real persisted suite results. `test_training_flow_eval_gate.py` holds the flow.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result, set_eval_gate  # noqa: E402
from examlops.evaluation import gate as gate_mod  # noqa: E402
from examlops.evaluation.gate import promotion_refusal  # noqa: E402


def _gate(model="jpcp", mode="block"):
    set_eval_gate(model, "suite-a", [{"name": "accuracy", "min": 0.8}], mode=mode)


def _scores(model="jpcp", version="7", accuracy=0.95):
    record_eval_result("suite-a", model, {"accuracy": accuracy}, run_id=f"r-{version}-{accuracy}",
                       model_version=version)  # fmt: skip
    record_eval_result("suite-a", model, {"accuracy": 0.9}, run_id="base", alias="Production")


def test_no_gate_is_not_a_refusal():
    assert promotion_refusal(["JPCP", "jpcp"], "7") is None


def test_a_failing_block_gate_refuses_and_is_audited():
    _gate()
    _scores(accuracy=0.5)

    reason = promotion_refusal(["JPCP", "jpcp"], "7", actor="pipeline")

    assert reason is not None and "accuracy" in reason
    events = [e for e in export_audit_events() if e["action"] == "promotion_blocked_by_gate"]
    assert events and events[-1]["target"] == "jpcp" and events[-1]["source"] == "pipeline"


def test_a_passing_gate_is_not_a_refusal():
    _gate()
    _scores(accuracy=0.95)

    assert promotion_refusal(["JPCP", "jpcp"], "7") is None


def test_a_warn_gate_never_refuses():
    _gate(mode="warn")
    _scores(accuracy=0.5)

    assert promotion_refusal(["jpcp"], "7") is None


def test_the_gate_is_found_under_either_name():
    """Operators configure gates under the registry name or the MLflow name."""
    _gate(model="JPCP")
    _scores(model="JPCP", accuracy=0.5)

    assert promotion_refusal(["JPCP", "jpcp"], "7") is not None
    assert promotion_refusal(["jpcp"], "7") is None  # not configured under that name


def test_a_gate_that_cannot_run_refuses(monkeypatch):
    """ADR 0008 clause 2: a gate that could not run is reported, never passed over."""
    _gate()

    def broken(*a, **k):
        raise RuntimeError("suite results table unreadable")

    monkeypatch.setattr(gate_mod, "run_eval_gate", broken)
    reason = promotion_refusal(["jpcp"], "7")

    assert reason is not None and "could not run" in reason
    assert any(e["action"] == "promotion_gate_error" for e in export_audit_events())


def test_missing_candidate_scores_refuse_rather_than_pass():
    """A candidate the suite never scored has not demonstrated anything."""
    _gate()
    _scores(version="6", accuracy=0.95)  # results for another version only

    assert promotion_refusal(["jpcp"], "7") is not None


# ── the flow consults the gate before it moves or announces an alias ─────────


def _promote_task_loop() -> ast.For:
    tree = ast.parse((ROOT / "pipelines" / "pipeline_generator.py").read_text())
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "promote_task"
    )
    return next(n for n in ast.walk(fn) if isinstance(n, ast.For))


def _first_line(node: ast.AST, name: str) -> int | None:
    lines = [
        c.lineno
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and getattr(c.func, "attr", getattr(c.func, "id", None)) == name
    ]
    return min(lines) if lines else None


def test_promote_task_asks_the_gate_before_moving_or_announcing():
    """A static guard (runs without the use-case pack): in the stage loop the gate is consulted
    before `set_registered_model_alias` and before `_announce_alias`, so a refused stage is
    neither moved nor published as moved."""
    loop = _promote_task_loop()
    asked = _first_line(loop, "_eval_gate_refusal")
    moved = _first_line(loop, "set_registered_model_alias")
    announced = _first_line(loop, "_announce_alias")

    assert asked is not None, "promote_task no longer consults the eval gate"
    assert moved is not None and asked < moved
    if announced is not None:  # the flow announces alias moves (ADR 0124) where that has landed
        assert asked < announced
    assert any(isinstance(n, ast.Break) for n in ast.walk(loop)), "a refusal must stop the walk"


@pytest.mark.parametrize("stage", ["Staging"])
def test_the_candidate_stage_is_not_gated(stage):
    """Gating Staging would stop a fresh version from ever being evaluated."""
    source = ast.unparse(_promote_task_loop())
    assert f"if stage != '{stage}':" in source

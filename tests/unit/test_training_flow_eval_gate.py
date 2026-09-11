# tests/unit/test_training_flow_eval_gate.py
"""BL-056 — the training flow's lifecycle promotion answers to the ADR 0008 eval gate.

Drives the real `promote_task` with a multi-stage lifecycle (Staging → Canary → Production) and
the real gate decision; only MLflow is a mock. Needs the use-case pack (skipped without it, like
the other flow tests); `test_promotion_refusal.py` holds the decision and a static guard over the
flow in every environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (_ROOT / "modelzoo"))
for _p in (str(_ROOT), str(_MZ), str(_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if not (_MZ / "seanergys_modelzoo").is_dir():
    pytest.skip("seanergys_modelzoo not present", allow_module_level=True)

import pipelines.pipeline_generator as pg  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result, set_eval_gate  # noqa: E402

_STAGES = ["Staging", "Canary", "Production"]
# Alias-move announcements (ADR 0124) may land before or after this; when the flow has them, a
# refused stage must not be announced either.
_ANNOUNCES = hasattr(pg, "_announce_alias")


def _config():
    cfg = MagicMock()
    cfg.get_inference_params.return_value = {
        "model_id": "gated",
        "lifecycle": [
            {"name": s, "metric": "rmse", "threshold": 50.0, "direction": "lower_is_better"}
            for s in _STAGES
        ],
    }
    return cfg


def _client():
    client = MagicMock()
    client.get_model_version_by_alias.side_effect = Exception("no Production yet")
    return client


def _promote(version="7", rmse=10.0):
    client = _client()
    announced: list[str] = []
    with (
        patch.dict(pg.MODEL_REGISTRY, {"GATED": (MagicMock(), _config(), {})}),
        patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client),
        patch.object(
            pg, "_announce_alias", lambda m, alias, v, p: announced.append(alias), create=True
        ),
        patch.object(pg, "_notify_ray_serve", lambda m: None),
    ):
        status = pg.promote_task.fn("GATED", {"version": version}, {"rmse": rmse})
    moved = [c.args[1] for c in client.set_registered_model_alias.call_args_list]
    return status, moved, announced


def _gate(mode="block", accuracy=0.95, version="7"):
    # A suite metric declares its own direction: the flow's fallback is the lifecycle rule's
    # (rmse, lower-is-better) — the same fallback `exa pipeline promote` derives from its rule.
    set_eval_gate(
        "gated", "suite-g", [{"name": "accuracy", "min": 0.8}], mode=mode, higher_is_better=True
    )
    record_eval_result("suite-g", "gated", {"accuracy": accuracy}, run_id=f"c{accuracy}",
                       model_version=version)  # fmt: skip
    record_eval_result("suite-g", "gated", {"accuracy": 0.9}, run_id="b", alias="Production")


def test_a_failing_block_gate_stops_the_flow_at_staging():
    _gate(accuracy=0.5)

    status, moved, announced = _promote()

    assert moved == ["Staging"], "no live alias moved"
    if _ANNOUNCES:
        assert announced == ["Staging"], "a refused stage is not announced"
    assert status == "Staging"
    assert any(e["action"] == "promotion_blocked_by_gate" for e in export_audit_events())


def test_a_passing_gate_lets_every_stage_through():
    _gate(accuracy=0.95)

    status, moved, announced = _promote()

    assert moved == _STAGES and status == "Production"
    if _ANNOUNCES:
        assert announced == _STAGES


def test_the_gate_runs_once_per_version():
    """Canary and Production share one verdict: two runs would persist two reports for it."""
    _gate(accuracy=0.95)
    with patch("examlops.evaluation.gate.run_eval_gate", return_value=None) as run:
        _promote()
    assert run.call_count == 1


def test_a_lower_is_better_lifecycle_is_the_fallback_direction():
    """Without a declared direction the gate takes the lifecycle rule's — as `promote` does."""
    set_eval_gate("gated", "suite-g", [{"name": "latency_ms", "max_drop": 5.0}], mode="block")
    record_eval_result("suite-g", "gated", {"latency_ms": 120.0}, run_id="c", model_version="7")
    record_eval_result("suite-g", "gated", {"latency_ms": 100.0}, run_id="b", alias="Production")

    assert _promote()[1] == ["Staging"], "latency rose 20 ms > 5 ms tolerated: refused"


def test_a_warn_gate_and_no_gate_leave_the_flow_unchanged():
    assert _promote()[1] == _STAGES  # no gate configured

    _gate(mode="warn", accuracy=0.5)
    assert _promote(version="8")[1] == _STAGES

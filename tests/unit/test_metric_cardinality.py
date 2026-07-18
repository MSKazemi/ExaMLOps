"""Metric label-budget guard (enterprise-readiness item 3.2 / QW5).

The Ray Serve hot-path metrics must never carry an unbounded label. `version` is the classic
cardinality bomb — a fresh time series on every MLflow promotion, multiplied by model × alias ×
status. This guard fails the build if `version` (or any other banned high-cardinality label)
creeps back onto the hot-path counter/histogram. Live version stays observable as the *value* of
`examlops_model_version` (a bounded model×alias gauge), so nothing is lost.

Source-AST based (not runtime) because the Ray deployment class can't be instantiated without a
Ray runtime; the label set is a static literal in the constructor, so parsing it is exact.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).parents[2] / "serving" / "ray_serving" / "app.py"

# Metric name → labels that must NEVER appear on it (unbounded on the hot path).
_BANNED_LABELS = {
    "examlops_predict_requests_total": {"version"},
    "examlops_predict_latency_seconds": {"version"},
}


def _tag_keys_by_metric() -> dict[str, set[str]]:
    """Map each Counter/Histogram/Gauge metric name → its declared tag_keys set."""
    tree = ast.parse(_APP.read_text())
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"Counter", "Histogram", "Gauge"}:
            continue
        # First positional arg is the metric name literal.
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        tag_keys = next(
            (kw.value for kw in node.keywords if kw.arg == "tag_keys"),
            None,
        )
        keys: set[str] = set()
        if isinstance(tag_keys, (ast.Tuple, ast.List)):
            keys = {e.value for e in tag_keys.elts if isinstance(e, ast.Constant)}
        found[name] = keys
    return found


def test_hot_path_metrics_exclude_unbounded_labels():
    metrics = _tag_keys_by_metric()
    for metric, banned in _BANNED_LABELS.items():
        assert metric in metrics, f"expected metric {metric!r} not found in app.py"
        offending = metrics[metric] & banned
        assert not offending, (
            f"{metric} carries banned high-cardinality label(s) {offending} — "
            "keep version as the value of examlops_model_version, not a label (item 3.2/QW5)"
        )


def test_version_gauge_present_for_observability():
    """Dropping the label must not drop observability: the bounded gauge must exist."""
    metrics = _tag_keys_by_metric()
    assert "examlops_model_version" in metrics
    assert metrics["examlops_model_version"] == {"model_name", "alias"}

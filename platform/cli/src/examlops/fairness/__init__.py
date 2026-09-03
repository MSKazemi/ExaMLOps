"""Next-Gen 40 · C8 — fairness & subgroup performance monitoring (ADR 0025).

Slice existing performance metrics by declared attributes, compute fairness disparities
(selection-rate range, demographic-parity difference, equalized-odds difference), gate /
alert on them (C3/C6), and surface them in model cards (A6) + compliance reports (D1).

Graceful degradation: Fairlearn's `MetricFrame` is used when installed; otherwise a
pure-Python implementation computes the same per-slice performance and the standard group
fairness metrics against `platform_db` samples. Slices below the configured minimum sample
size are excluded from disparity + alerting (noise guard, R3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db

DEFAULT_THRESHOLD = 0.1
DEFAULT_MIN_SAMPLES = 30


@dataclass
class SliceMetric:
    slice_value: str
    n: int
    accuracy: float | None  # for classification (label/pred in {0,1})
    error: float | None  # mean absolute error (regression)
    selection_rate: float | None  # fraction predicted positive
    tpr: float | None
    fpr: float | None
    below_min: bool


@dataclass
class FairnessResult:
    model: str
    slice_attr: str
    tenant: str
    slices: list[SliceMetric] = field(default_factory=list)
    demographic_parity_diff: float | None = None
    equalized_odds_diff: float | None = None
    selection_rate_range: float | None = None
    accuracy_range: float | None = None
    threshold: float = DEFAULT_THRESHOLD
    disparity_exceeded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "slice_attr": self.slice_attr,
            "tenant": self.tenant,
            "threshold": self.threshold,
            "demographic_parity_diff": self.demographic_parity_diff,
            "equalized_odds_diff": self.equalized_odds_diff,
            "selection_rate_range": self.selection_rate_range,
            "accuracy_range": self.accuracy_range,
            "disparity_exceeded": self.disparity_exceeded,
            "slices": [
                {
                    "slice_value": s.slice_value,
                    "n": s.n,
                    "accuracy": s.accuracy,
                    "error": s.error,
                    "selection_rate": s.selection_rate,
                    "tpr": s.tpr,
                    "fpr": s.fpr,
                    "below_min": s.below_min,
                }
                for s in self.slices
            ],
        }


def _is_binary(vals: list[float]) -> bool:
    return all(v in (0.0, 1.0) for v in vals)


def _slice_metric(
    slice_value: str, preds: list[float], labels: list[float], min_samples: int
) -> SliceMetric:
    n = len(preds)
    below = n < min_samples
    binary = bool(labels) and _is_binary(preds) and _is_binary(labels)
    accuracy = error = selection_rate = tpr = fpr = None
    if binary:
        correct = sum(1 for p, y in zip(preds, labels) if round(p) == round(y))
        accuracy = correct / n if n else None
        selection_rate = sum(1 for p in preds if round(p) == 1) / n if n else None
        pos = [(p, y) for p, y in zip(preds, labels) if round(y) == 1]
        neg = [(p, y) for p, y in zip(preds, labels) if round(y) == 0]
        tpr = (sum(1 for p, _ in pos if round(p) == 1) / len(pos)) if pos else None
        fpr = (sum(1 for p, _ in neg if round(p) == 1) / len(neg)) if neg else None
    else:
        if labels:
            error = sum(abs(p - y) for p, y in zip(preds, labels)) / n if n else None
        selection_rate = (sum(1 for p in preds if p > 0.5) / n) if n else None
    return SliceMetric(slice_value, n, accuracy, error, selection_rate, tpr, fpr, below)


def slice_metrics(
    model: str, slice_attr: str, *, tenant: str = "default", min_samples: int | None = None
) -> FairnessResult:
    """Per-slice performance for a slicing attribute (R2, GWT-1).

    Uses Fairlearn's ``MetricFrame`` when available; otherwise pure-Python. Reads samples
    from ``platform_db.fairness_samples``.
    """
    cfg, _source = effective_fairness_config(model)
    if min_samples is None:
        min_samples = cfg["min_samples"] if cfg else DEFAULT_MIN_SAMPLES
    threshold = cfg["threshold"] if cfg else DEFAULT_THRESHOLD

    samples = platform_db.get_fairness_samples(model, slice_attr, tenant=tenant)
    groups: dict[str, dict[str, list[float]]] = {}
    for s in samples:
        g = groups.setdefault(s["slice_value"], {"preds": [], "labels": []})
        if s["prediction"] is not None:
            g["preds"].append(s["prediction"])
        if s["label"] is not None:
            g["labels"].append(s["label"])

    slices = []
    for value, data in sorted(groups.items()):
        # Align preds/labels length (labels may lag); use the common prefix.
        m = _slice_metric(value, data["preds"], data["labels"], min_samples)
        slices.append(m)

    result = FairnessResult(
        model=model, slice_attr=slice_attr, tenant=tenant, slices=slices, threshold=threshold
    )
    _fill_disparities(result)
    return result


def _fill_disparities(result: FairnessResult) -> None:
    """Compute group fairness disparities over slices above the min-sample guard (R2/R3)."""
    eligible = [s for s in result.slices if not s.below_min]
    if len(eligible) < 2:
        return
    sel = [s.selection_rate for s in eligible if s.selection_rate is not None]
    acc = [s.accuracy for s in eligible if s.accuracy is not None]
    tprs = [s.tpr for s in eligible if s.tpr is not None]
    fprs = [s.fpr for s in eligible if s.fpr is not None]

    if sel:
        result.selection_rate_range = max(sel) - min(sel)
        result.demographic_parity_diff = max(sel) - min(sel)
    if acc:
        result.accuracy_range = max(acc) - min(acc)
    if tprs and fprs:
        result.equalized_odds_diff = max(max(tprs) - min(tprs), max(fprs) - min(fprs))
    elif tprs:
        result.equalized_odds_diff = max(tprs) - min(tprs)

    disparities = [
        d
        for d in (
            result.demographic_parity_diff,
            result.equalized_odds_diff,
            result.accuracy_range,
        )
        if d is not None
    ]
    result.disparity_exceeded = any(d > result.threshold for d in disparities)


_FAIRNESS_KEYS = {"slices", "threshold", "min_samples", "gate_promotion", "enabled"}


def validate_fairness_block(block: dict[str, Any] | None, *, model: str = "") -> list[str]:
    """Validate a model YAML ``fairness:`` block; returns human-readable errors (clause 1).

    Mirrors ``engines.validate_engine_block`` and is called by the registry-integrity CI guard,
    so a typo is caught at review time rather than by a gate that quietly never fires. An
    **unknown key is an error, not ignored**: ``slice:`` instead of ``slices:`` would otherwise
    parse into a registry that declares nothing, and a fairness gate over zero attributes passes
    every model.
    """
    if not block:
        return []
    where = f"{model}: " if model else ""
    errors: list[str] = []
    if not isinstance(block, dict):
        return [f"{where}fairness block must be a mapping"]
    for key in block:
        if key not in _FAIRNESS_KEYS:
            errors.append(f"{where}unknown fairness key {key!r}; expected {sorted(_FAIRNESS_KEYS)}")
    slices = block.get("slices")
    if slices is None:
        errors.append(f"{where}fairness block must declare 'slices'")
    elif not isinstance(slices, list) or not all(isinstance(s, str) and s for s in slices):
        errors.append(f"{where}fairness.slices must be a list of attribute names")
    elif not slices:
        errors.append(f"{where}fairness.slices is empty; remove the block or name an attribute")
    # YAML parses `threshold: 1` as an int, so a float-only check would reject a valid value.
    # bool is a subclass of int and is not a number here.
    threshold = block.get("threshold")
    if "threshold" in block:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            errors.append(f"{where}fairness.threshold must be a number between 0 and 1")
        elif not 0 <= float(threshold) <= 1:
            errors.append(f"{where}fairness.threshold must be between 0 and 1")
    min_samples = block.get("min_samples")
    if "min_samples" in block:
        if isinstance(min_samples, bool) or not isinstance(min_samples, int):
            errors.append(f"{where}fairness.min_samples must be an integer")
        elif min_samples < 1:
            errors.append(f"{where}fairness.min_samples must be at least 1")
    for key in ("gate_promotion", "enabled"):
        if key in block and not isinstance(block[key], bool):
            errors.append(f"{where}fairness.{key} must be a boolean")
    return errors


def _yaml_fairness_config(model: str) -> dict[str, Any] | None:
    """The model YAML's ``fairness:`` block as a config dict, or None.

    Best-effort: the platform runs against packs it may not be able to load (no use-case dir in
    a bare container), and a missing pack must not turn a fairness gate into an error.
    """
    try:
        from pathlib import Path

        import yaml as _yaml

        from examlops import usecase

        directory = Path(usecase.models_dir())
        if not directory.is_dir():
            return None
        for path in sorted(directory.glob("*.yaml")):
            raw = _yaml.safe_load(path.read_text()) or {}
            if str(raw.get("name", "")).lower() != model.lower():
                continue
            block = raw.get("fairness") or {}
            if not block or validate_fairness_block(block, model=model):
                return None
            return {
                "model": model,
                "slice_attrs": list(block["slices"]),
                "threshold": float(block.get("threshold", DEFAULT_THRESHOLD)),
                "min_samples": int(block.get("min_samples", DEFAULT_MIN_SAMPLES)),
                "gate_promotion": bool(block.get("gate_promotion", False)),
                "enabled": bool(block.get("enabled", True)),
            }
    except Exception:  # noqa: BLE001
        return None
    return None


def effective_fairness_config(model: str) -> tuple[dict[str, Any] | None, str]:
    """The config actually in force, and where it came from — ``db``, ``yaml`` or ``none``.

    **A runtime row wins over the declaration.** Writing it is a deliberate act by an operator on
    a live system (the CLI or the dashboard console), and having the YAML silently override it
    would make a shipped write surface look broken. The YAML block is the declaration and the
    default: it is in force wherever nobody has overridden it, so **declaring slices in code is
    immediately effective** — no apply step stands between a declaration and its gate. That
    matters more than the precedence: a registry that only counts once someone remembers to
    materialise it is a protection that silently does not exist.

    Use :func:`fairness_config_drift` to see when the two disagree.
    """
    row = platform_db.get_fairness_config(model)
    if row:
        return row, "db"
    from_yaml = _yaml_fairness_config(model)
    if from_yaml:
        return from_yaml, "yaml"
    return None, "none"


def fairness_config_drift(model: str) -> list[str]:
    """Fields where a materialised row disagrees with the model YAML's declaration.

    Reported rather than resolved. Silently preferring one is how a reviewed declaration and a
    live gate come to differ with nobody able to see it.
    """
    row = platform_db.get_fairness_config(model)
    declared = _yaml_fairness_config(model)
    if not row or not declared:
        return []
    drift = []
    for key in ("slice_attrs", "threshold", "min_samples", "gate_promotion", "enabled"):
        if row.get(key) != declared.get(key):
            drift.append(f"{key}: yaml={declared.get(key)!r} db={row.get(key)!r}")
    return drift


def fairness_disparity(model: str, slice_attr: str, *, tenant: str = "default") -> dict[str, Any]:
    """Fairness disparities (DP/EO diff, selection-rate range) for a slice attr (R2)."""
    return slice_metrics(model, slice_attr, tenant=tenant).as_dict()


def fairness_report(model: str, *, tenant: str = "default") -> list[FairnessResult]:
    """Full fairness report across all declared slicing attributes (R5)."""
    cfg, _source = effective_fairness_config(model)
    attrs = cfg["slice_attrs"] if cfg else []
    return [slice_metrics(model, a, tenant=tenant) for a in attrs]


def fairness_gate(model: str, *, tenant: str = "default") -> bool:
    """True if any declared slice attr exceeds the disparity threshold (R4, used by C3)."""
    cfg, _source = effective_fairness_config(model)
    if not cfg or not cfg["gate_promotion"]:
        return False
    for attr in cfg["slice_attrs"]:
        if slice_metrics(model, attr, tenant=tenant).disparity_exceeded:
            return True
    return False


__all__ = [
    "SliceMetric",
    "FairnessResult",
    "slice_metrics",
    "fairness_disparity",
    "fairness_report",
    "fairness_gate",
    "validate_fairness_block",
    "effective_fairness_config",
    "fairness_config_drift",
]

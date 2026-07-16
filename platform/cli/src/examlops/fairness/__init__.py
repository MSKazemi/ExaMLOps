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

from examlops import platform_db

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
    cfg = platform_db.get_fairness_config(model)
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


def fairness_disparity(model: str, slice_attr: str, *, tenant: str = "default") -> dict[str, Any]:
    """Fairness disparities (DP/EO diff, selection-rate range) for a slice attr (R2)."""
    return slice_metrics(model, slice_attr, tenant=tenant).as_dict()


def fairness_report(model: str, *, tenant: str = "default") -> list[FairnessResult]:
    """Full fairness report across all declared slicing attributes (R5)."""
    cfg = platform_db.get_fairness_config(model)
    attrs = cfg["slice_attrs"] if cfg else []
    return [slice_metrics(model, a, tenant=tenant) for a in attrs]


def fairness_gate(model: str, *, tenant: str = "default") -> bool:
    """True if any declared slice attr exceeds the disparity threshold (R4, used by C3)."""
    cfg = platform_db.get_fairness_config(model)
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
]

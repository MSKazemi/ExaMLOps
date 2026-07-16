"""Next-Gen 40 · C5 — advanced drift: concept, label-free perf, data quality (ADR 0022).

Extends the existing drift subsystem (feature / prediction / input-embedding) with
three new detectors, all unified under a ``drift_kind`` discriminator in the
``drift_events`` table and consumable by the existing auto-retrain trigger:

- **concept drift** (``drift_kind=concept``) — as delayed labels arrive, track the
  realized error metric over time and compare a recent window against a baseline
  window; a large shift in the input→target relationship is concept drift.
- **label-free performance estimation** (``perf_estimates``) — a CBPE-like estimate of
  accuracy from prediction confidence *before* labels arrive; a large drop **warns**
  (never force-retrains) until labels confirm.
- **data-quality profiling** (``drift_kind=data_quality``) — schema / null / range /
  cardinality profile of an inference batch, incorporating A5 bad-payload counters.

Graceful degradation: Evidently / River / NannyML / whylogs are all optional. Absent
them, pure-Python statistics (mean-shift z-test, confidence-based estimate, null/range
profile) provide the same signals against ``platform_db``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from examlops import platform_db

# Severity thresholds (relative error increase for concept; null fraction for quality).
CONCEPT_WARN_Z = 2.0
CONCEPT_CRITICAL_Z = 3.0
PERF_WARN_DROP = 0.10  # 10% estimated-accuracy drop vs baseline => warn
QUALITY_NULL_WARN = 0.20
QUALITY_NULL_CRITICAL = 0.50


@dataclass(frozen=True)
class DriftResult:
    model: str
    drift_kind: str
    severity: str  # OK | WARN | CRITICAL
    score: float | None = None
    metric: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_critical(self) -> bool:
        return self.severity == "CRITICAL"


@dataclass(frozen=True)
class QualityProfile:
    model: str
    n: int
    fields: dict[str, dict[str, Any]]  # per-field: nulls, min, max, cardinality
    null_fraction: float
    severity: str


def _severity_from_z(z: float) -> str:
    if z >= CONCEPT_CRITICAL_Z:
        return "CRITICAL"
    if z >= CONCEPT_WARN_Z:
        return "WARN"
    return "OK"


def _abs_error(pred: float, label: float) -> float:
    return abs(pred - label)


def detect_concept_drift(
    model: str,
    *,
    alias: str | None = None,
    window: int = 50,
    metric: str = "abs_error",
    persist: bool = True,
) -> DriftResult:
    """Concept-drift test on realized error as labels arrive (R1).

    Splits the labelled prediction stream into a *baseline* head and a *recent*
    tail of ``window`` points, then runs a mean-shift z-test on per-sample error.
    A significant increase in realized error signals a changed input→target
    relationship. Records ``drift_kind=concept`` (R6); severity is auto-retrain
    consumable (R2).
    """
    pairs = platform_db.join_predictions_with_truth(model, alias)
    errors = [_abs_error(p["prediction"], p["label"]) for p in pairs]
    n = len(errors)
    if n < 2 * min(window, 10):  # not enough labelled data yet
        res = DriftResult(
            model, "concept", "OK", metric=metric, detail={"n": n, "reason": "insufficient labels"}
        )
        if persist:
            _persist(res)
        return res

    w = min(window, n // 2)
    baseline, recent = errors[:-w], errors[-w:]
    b_mean = sum(baseline) / len(baseline)
    b_var = sum((e - b_mean) ** 2 for e in baseline) / max(len(baseline) - 1, 1)
    b_std = math.sqrt(b_var) or 1e-9
    r_mean = sum(recent) / len(recent)
    # One-sided z of the recent mean under the baseline distribution of the mean.
    z = (r_mean - b_mean) / (b_std / math.sqrt(len(recent)))
    z = max(z, 0.0)  # only error *increases* are concept drift
    severity = _severity_from_z(z)
    res = DriftResult(
        model,
        "concept",
        severity,
        score=z,
        metric=metric,
        detail={"baseline_error": b_mean, "recent_error": r_mean, "window": w, "n": n},
    )
    if persist:
        _persist(res)
    return res


def estimate_performance(
    model: str,
    *,
    alias: str | None = None,
    metric: str = "accuracy",
    baseline: float | None = None,
    window: int = 200,
    persist: bool = True,
) -> dict[str, Any]:
    """Label-free performance estimate (CBPE-like) before labels arrive (R3/R4).

    For classification-style predictions in ``[0, 1]`` (probabilities), expected
    accuracy under calibration is ``mean(max(p, 1-p))`` — the model's own confidence.
    A large drop vs ``baseline`` **warns** but never forces a retrain. Realized
    accuracy (if any labels exist) is stored alongside for the estimated-vs-realized
    panel.
    """
    preds = _read_recent_predictions(model, alias, window)
    values = [p["prediction"] for p in preds]
    if not values:
        return {"model": model, "metric": metric, "estimated": None, "realized": None, "n": 0}

    in_unit = all(0.0 <= v <= 1.0 for v in values)
    if in_unit:  # CBPE for probabilistic classification
        estimated = sum(max(v, 1.0 - v) for v in values) / len(values)
    else:  # regression fallback: stability-based proxy (1 / (1 + normalized spread))
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
        spread = math.sqrt(var) / (abs(mean) + 1e-9)
        estimated = 1.0 / (1.0 + spread)

    # Realized (if any labels landed) — accuracy for unit preds, else neg-MAE proxy.
    pairs = platform_db.join_predictions_with_truth(model, alias)
    realized: float | None = None
    if pairs:
        if in_unit:
            realized = sum(1 for p in pairs if round(p["prediction"]) == round(p["label"])) / len(
                pairs
            )
        else:
            mae = sum(_abs_error(p["prediction"], p["label"]) for p in pairs) / len(pairs)
            realized = 1.0 / (1.0 + mae)

    drop = (baseline - estimated) if baseline is not None else 0.0
    warn = drop >= PERF_WARN_DROP
    if persist:
        platform_db.record_perf_estimate(
            model, metric, estimated=estimated, realized=realized, baseline=baseline
        )
        if warn:  # R4 — warn only, never force-retrain
            platform_db.record_drift_event(
                model,
                "concept",
                severity="WARN",
                score=drop,
                metric=metric,
                detail={"estimated": estimated, "baseline": baseline, "label_free": True},
            )
    return {
        "model": model,
        "metric": metric,
        "estimated": estimated,
        "realized": realized,
        "baseline": baseline,
        "warn": warn,
        "n": len(values),
        "method": "cbpe-like" if in_unit else "stability-proxy",
    }


def _read_recent_predictions(model: str, alias: str | None, limit: int) -> list[dict[str, Any]]:
    sql = "SELECT prediction, request_hash FROM predictions WHERE model=?"
    params: list[Any] = [model]
    if alias:
        sql += " AND alias=?"
        params.append(alias)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with platform_db.get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _persist(res: DriftResult) -> None:
    platform_db.record_drift_event(
        res.model,
        res.drift_kind,
        severity=res.severity,
        score=res.score,
        metric=res.metric,
        detail=res.detail,
    )


def profile_inference(
    model: str,
    batch: Sequence[dict[str, Any]],
    *,
    bad_payloads: int = 0,
    persist: bool = True,
) -> QualityProfile:
    """Profile an inference batch: schema / nulls / ranges / cardinality (R5).

    Records ``drift_kind=data_quality`` and folds in A5 ``bad_payloads`` counters.
    A null-fraction spike escalates severity (WARN/CRITICAL).
    """
    n = len(batch)
    fields: dict[str, dict[str, Any]] = {}
    total_cells = 0
    null_cells = 0
    for row in batch:
        for k, v in row.items():
            f = fields.setdefault(
                k, {"nulls": 0, "values": set(), "min": None, "max": None, "count": 0}
            )
            f["count"] += 1
            total_cells += 1
            if v is None or (isinstance(v, float) and math.isnan(v)):
                f["nulls"] += 1
                null_cells += 1
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                f["min"] = v if f["min"] is None else min(f["min"], v)
                f["max"] = v if f["max"] is None else max(f["max"], v)
            f["values"].add(v if isinstance(v, (int, float, str, bool)) else str(v))

    summary: dict[str, dict[str, Any]] = {}
    for k, f in fields.items():
        summary[k] = {
            "nulls": f["nulls"],
            "null_fraction": f["nulls"] / f["count"] if f["count"] else 0.0,
            "min": f["min"],
            "max": f["max"],
            "cardinality": len(f["values"]),
        }

    denom = total_cells + bad_payloads
    null_fraction = (null_cells + bad_payloads) / denom if denom else 0.0
    if null_fraction >= QUALITY_NULL_CRITICAL:
        severity = "CRITICAL"
    elif null_fraction >= QUALITY_NULL_WARN:
        severity = "WARN"
    else:
        severity = "OK"

    prof = QualityProfile(model, n, summary, null_fraction, severity)
    if persist:
        platform_db.record_drift_event(
            model,
            "data_quality",
            severity=severity,
            score=null_fraction,
            detail={"n": n, "bad_payloads": bad_payloads, "fields": summary},
        )
    return prof


__all__ = [
    "DriftResult",
    "QualityProfile",
    "detect_concept_drift",
    "estimate_performance",
    "profile_inference",
]

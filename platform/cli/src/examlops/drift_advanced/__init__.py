"""Next-Gen 40 · C5 — advanced drift: concept, label-free perf, data quality (ADR 0022).

Extends the existing drift subsystem (feature / prediction / input-embedding) with
three new detectors, all unified under a ``drift_kind`` discriminator in the
``drift_events`` table and consumable by the existing auto-retrain trigger:

- **concept drift** (``drift_kind=concept``) — as delayed labels arrive, track the
  realized error metric over time and compare a recent window against a baseline
  window; a large shift in the input→target relationship is concept drift.
- **label-free performance estimation** (``perf_estimates``) — an estimate of accuracy
  *before* labels arrive; a large drop **warns** (never force-retrains) until realized labels in
  the recent window confirm it, and a confirmed drop is ``CRITICAL`` — the estimated-performance
  signal :func:`concept_retrain_signal` hands the cooldown-aware ``exa drift trigger``.
- **data-quality profiling** (``drift_kind=data_quality``) — schema / null / range /
  cardinality profile of an inference batch, folding in the A5 contract rejections the
  inference ingress records (``inference_rejections``).

Each of the three is a seam with a pure-Python default and optional adapters for the libraries
ADR 0022 names (:mod:`examlops.drift_advanced.adapters`, extra ``examlops[drift-advanced]``):

* concept — :data:`CONCEPT_DETECTORS` / ``EXAMLOPS_DRIFT_CONCEPT_DETECTOR``: ``builtin``
  (mean-shift z-test, default), ``ddm`` (pure-Python DDM), ``river-adwin``, ``river-ddm``,
  ``evidently``;
* estimate — :data:`PERF_ESTIMATORS` / ``EXAMLOPS_DRIFT_PERF_ESTIMATOR``: ``builtin``
  (CBPE-like confidence, default), ``nannyml`` (CBPE for probabilistic classifiers, DLE for
  regression);
* quality — :data:`QUALITY_PROFILERS` / ``EXAMLOPS_DRIFT_QUALITY_PROFILER``: ``builtin``
  (default), ``whylogs``.

An adapter whose library is missing, cannot import, or fails on the data degrades to the builtin
and records why (``detector_fallback`` / ``estimator_fallback`` / ``profiler_fallback``) — a
detector that silently became a different one would make the recorded name a lie.

:mod:`examlops.drift_advanced.scheduler` runs the detectors over every model on a schedule.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db
from examlops.drift_advanced import adapters

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
    bad_payloads: int = 0
    profiler: str = "builtin"
    profiler_fallback: str | None = None


def _severity_from_z(z: float) -> str:
    if z >= CONCEPT_CRITICAL_Z:
        return "CRITICAL"
    if z >= CONCEPT_WARN_Z:
        return "WARN"
    return "OK"


def _abs_error(pred: float, label: float) -> float:
    return abs(pred - label)


#: A concept detector maps ``(baseline errors, recent errors)`` to ``(severity, score, extra)``.
ConceptDetector = Callable[[list[float], list[float]], tuple[str, float, dict[str, Any]]]

CONCEPT_DETECTOR_ENV = "EXAMLOPS_DRIFT_CONCEPT_DETECTOR"
DEFAULT_CONCEPT_DETECTOR = "builtin"


def _builtin_concept(baseline: list[float], recent: list[float]) -> tuple[str, float, dict]:
    """One-sided mean-shift z-test of the recent error mean under the baseline distribution."""
    b_mean = sum(baseline) / len(baseline)
    b_var = sum((e - b_mean) ** 2 for e in baseline) / max(len(baseline) - 1, 1)
    b_std = math.sqrt(b_var) or 1e-9
    r_mean = sum(recent) / len(recent)
    z = (r_mean - b_mean) / (b_std / math.sqrt(len(recent)))
    z = max(z, 0.0)  # only error *increases* are concept drift
    return (
        _severity_from_z(z),
        z,
        {"baseline_error": b_mean, "recent_error": r_mean},
    )


def _river_adwin_concept(baseline: list[float], recent: list[float]) -> tuple[str, float, dict]:
    """River ADWIN over the error stream (lazy import; raises ImportError when River is absent).

    The whole labelled stream is fed in order. Drift counts only if ADWIN signalled **inside the
    recent window** *and* the recent error is above the baseline (a fall in error is an
    improvement, not a concept shift). ADWIN gives a decision, not a magnitude, so the score is the
    builtin z and severity is ``CRITICAL`` on a confirmed drift, else whatever z alone implies but
    never above ``WARN`` — the streaming detector is what may escalate to ``CRITICAL``.
    """
    from river.drift import ADWIN  # type: ignore[import-not-found]  # noqa: PLC0415

    sev, z, extra = _builtin_concept(baseline, recent)
    adwin = ADWIN()
    first_recent = len(baseline)
    detected_in_recent = False
    for i, err in enumerate(baseline + recent):
        adwin.update(err)
        if adwin.drift_detected and i >= first_recent:
            detected_in_recent = True
    increased = extra["recent_error"] > extra["baseline_error"]
    confirmed = detected_in_recent and increased
    severity = "CRITICAL" if confirmed else ("WARN" if sev != "OK" else "OK")
    extra["adwin_drift"] = detected_in_recent
    return severity, z, extra


def _confirmed(sev: str, confirmed: bool, warned: bool) -> str:
    """Severity for a decision-only detector: only its confirmed drift may reach ``CRITICAL``."""
    if confirmed:
        return "CRITICAL"
    return "WARN" if (warned or sev != "OK") else "OK"


def _ddm_with(detector: Any, name: str, baseline: list[float], recent: list[float]):
    sev, z, extra = _builtin_concept(baseline, recent)
    drift, warning = adapters.scan_ddm(
        detector, adapters.failure_bits(baseline, recent), len(baseline)
    )
    increased = extra["recent_error"] > extra["baseline_error"]
    extra[f"{name}_drift"] = drift
    extra[f"{name}_warning"] = warning
    return _confirmed(sev, drift and increased, warning), z, extra


def _ddm_concept(baseline: list[float], recent: list[float]) -> tuple[str, float, dict]:
    """DDM (Gama et al. 2004) over the error stream, pure Python — same rule as River's DDM.

    Errors are binarised against the baseline's 75th percentile (scale-free, so regression errors
    work too). A drift signalled inside the recent window with a higher recent error is
    ``CRITICAL``; a DDM warning or a z-test breach alone is at most ``WARN``.
    """
    return _ddm_with(adapters.PureDDM(), "ddm", baseline, recent)


def _river_ddm_concept(baseline: list[float], recent: list[float]) -> tuple[str, float, dict]:
    """River's ``DDM`` (lazy import; :class:`adapters.AdapterUnavailable` when River is absent)."""
    return _ddm_with(adapters.river_ddm(), "ddm", baseline, recent)


def _evidently_concept(baseline: list[float], recent: list[float]) -> tuple[str, float, dict]:
    """Evidently ``ValueDrift`` of recent vs baseline realized error (lazy import).

    A distribution shift counts only when the recent error is also *higher* — a model that got
    better has not suffered concept drift. Evidently's own statistic and method are recorded.
    """
    sev, z, extra = _builtin_concept(baseline, recent)
    verdict = adapters.evidently_value_drift(baseline, recent)
    increased = extra["recent_error"] > extra["baseline_error"]
    extra.update(
        {
            "evidently_drift": verdict["drift"],
            "evidently_method": verdict["method"],
            "evidently_value": verdict["value"],
            "evidently_threshold": verdict["threshold"],
        }
    )
    return _confirmed(sev, verdict["drift"] and increased, False), z, extra


#: Selectable concept detectors. Register another with ``CONCEPT_DETECTORS["name"] = fn``.
CONCEPT_DETECTORS: dict[str, ConceptDetector] = {
    "builtin": _builtin_concept,
    "ddm": _ddm_concept,
    "river-adwin": _river_adwin_concept,
    "river-ddm": _river_ddm_concept,
    "evidently": _evidently_concept,
}

#: Errors that mean "this adapter cannot run here" — the detector degrades to the builtin.
_UNAVAILABLE = (ImportError, adapters.AdapterUnavailable)


def _unavailable_reason(name: str, exc: BaseException) -> str:
    if isinstance(exc, adapters.AdapterUnavailable):
        return f"{name} unavailable: {exc}"
    return f"{name} is not installed"


def resolve_concept_detector(name: str | None = None) -> tuple[str, ConceptDetector, str | None]:
    """The detector to run: ``(name used, fn, fallback_reason)``.

    Unknown names and adapters whose library is missing degrade to ``builtin`` and say why — a
    detector that silently became a different one would make the recorded ``detector`` a lie.
    """
    want = (name or os.getenv(CONCEPT_DETECTOR_ENV) or DEFAULT_CONCEPT_DETECTOR).strip().lower()
    fn = CONCEPT_DETECTORS.get(want)
    if fn is None:
        return "builtin", _builtin_concept, f"unknown detector {want!r}"
    return want, fn, None


def detect_concept_drift(
    model: str,
    *,
    alias: str | None = None,
    window: int = 50,
    metric: str = "abs_error",
    persist: bool = True,
    detector: str | None = None,
) -> DriftResult:
    """Concept-drift test on realized error as labels arrive (R1).

    Splits the labelled prediction stream into a *baseline* head and a *recent*
    tail of ``window`` points and hands both to the concept detector (``builtin``
    mean-shift z-test by default; ``river-adwin`` when selected and installed).
    A significant increase in realized error signals a changed input→target
    relationship. Records ``drift_kind=concept`` (R6); severity is auto-retrain
    consumable (R2). The detector that actually ran is recorded in ``detail``.
    """
    pairs = labelled_pairs(model, alias)
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
    used, fn, reason = resolve_concept_detector(detector)
    try:
        severity, z, extra = fn(baseline, recent)
    except _UNAVAILABLE as exc:
        used, reason = "builtin", _unavailable_reason(used, exc)
        severity, z, extra = _builtin_concept(baseline, recent)
    except Exception as exc:  # noqa: BLE001 - a library bug must not stop the sweep, and says so
        used, reason = "builtin", f"{used} failed: {type(exc).__name__}: {exc}"
        severity, z, extra = _builtin_concept(baseline, recent)
    detail = {**extra, "window": w, "n": n, "detector": used}
    if reason:
        detail["detector_fallback"] = reason
    res = DriftResult(model, "concept", severity, score=z, metric=metric, detail=detail)
    if persist:
        _persist(res)
    return res


#: A label-free estimate is escalated to ``CRITICAL`` only once realized labels confirm it: at least
#: this many labelled points in the recent window, whose realized score also fell by
#: ``PERF_WARN_DROP`` against a comparable reference (see :func:`_realized_drop`) — ADR 0022:
#: "estimate only warns until confirmed".
PERF_CONFIRM_MIN_LABELS = 10
#: Upper bound on the labelled history read for a detector — a bounded query on a busy model.
MAX_LABELLED_PAIRS = 10_000

PerfEstimator = Callable[
    [list[float], list[dict[str, Any] | None], list[dict[str, Any]], bool],
    tuple[float, str, dict[str, Any]],
]

PERF_ESTIMATOR_ENV = "EXAMLOPS_DRIFT_PERF_ESTIMATOR"
DEFAULT_PERF_ESTIMATOR = "builtin"


def _builtin_estimate(
    values: list[float],
    _features: list[dict[str, Any] | None],
    _reference: list[dict[str, Any]],
    probabilistic: bool,
) -> tuple[float, str, dict[str, Any]]:
    """CBPE-like confidence estimate (classification) or a stability proxy (regression)."""
    if probabilistic:  # expected accuracy under calibration is the model's own confidence
        return sum(max(v, 1.0 - v) for v in values) / len(values), "cbpe-like", {}
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
    spread = math.sqrt(var) / (abs(mean) + 1e-9)
    return 1.0 / (1.0 + spread), "stability-proxy", {}


def _nannyml_estimate(
    values: list[float],
    features: list[dict[str, Any] | None],
    reference: list[dict[str, Any]],
    probabilistic: bool,
) -> tuple[float, str, dict[str, Any]]:
    """NannyML CBPE (probabilistic classification) / DLE (regression) — lazy import."""
    return adapters.nannyml_estimate(reference, values, features, probabilistic=probabilistic)


#: Selectable label-free estimators. Register another with ``PERF_ESTIMATORS["name"] = fn``.
PERF_ESTIMATORS: dict[str, PerfEstimator] = {
    "builtin": _builtin_estimate,
    "nannyml": _nannyml_estimate,
}


def resolve_perf_estimator(name: str | None = None) -> tuple[str, PerfEstimator, str | None]:
    """The estimator to run: ``(name used, fn, fallback_reason)`` — same contract as detectors."""
    want = (name or os.getenv(PERF_ESTIMATOR_ENV) or DEFAULT_PERF_ESTIMATOR).strip().lower()
    fn = PERF_ESTIMATORS.get(want)
    if fn is None:
        return "builtin", _builtin_estimate, f"unknown estimator {want!r}"
    return want, fn, None


def _realized(pairs: list[dict[str, Any]], probabilistic: bool) -> float | None:
    if not pairs:
        return None
    if probabilistic:
        hits = sum(1 for p in pairs if round(p["prediction"]) == round(p["label"]))
        return hits / len(pairs)
    mae = sum(_abs_error(p["prediction"], p["label"]) for p in pairs) / len(pairs)
    return 1.0 / (1.0 + mae)


def _realized_drop(
    reference: list[dict[str, Any]],
    realized: float | None,
    baseline: float | None,
    probabilistic: bool,
) -> tuple[float | None, str | None, float | None]:
    """How far the realized score fell: ``(drop, basis, realized_baseline)``.

    Confirmation must compare like with like. The estimate ``baseline`` is in the *estimator's*
    units — for a regressor under the builtin estimator that is the stability proxy
    ``1/(1+CV)``, which is not the realized ``1/(1+MAE)``: comparing the two confirmed almost every
    regression warning, because a stability proxy near 1 dwarfs a realized score for any MAE above
    a few units. So:

    * ``realized_reference`` — at least ``PERF_CONFIRM_MIN_LABELS`` labelled points older than the
      recent window exist: the drop is the **relative** fall of the realized score against the
      realized score of that older window (unit-free, so it means the same for accuracy and MAE).
    * ``baseline`` — no labelled reference, but the predictions are probabilities: the realized
      score and the baseline are both accuracies, so the absolute drop against it is meaningful.
    * otherwise ``None`` — nothing comparable to confirm against, so the estimate stays a warning.
    """
    if realized is None:
        return None, None, None
    if len(reference) >= PERF_CONFIRM_MIN_LABELS:
        ref = _realized(reference, probabilistic)
        if ref is not None and ref > 0:
            return (ref - realized) / ref, "realized_reference", ref
        return None, None, ref
    if probabilistic and baseline is not None:
        return baseline - realized, "baseline", baseline
    return None, None, None


def estimate_performance(
    model: str,
    *,
    alias: str | None = None,
    metric: str = "accuracy",
    baseline: float | None = None,
    window: int = 200,
    persist: bool = True,
    estimator: str | None = None,
) -> dict[str, Any]:
    """Label-free performance estimate before labels arrive, confirmed by them when they do.

    The estimator is a seam (``builtin`` CBPE-like confidence / stability proxy by default,
    ``nannyml`` CBPE/DLE when selected and installed; see :data:`PERF_ESTIMATORS`). Severity:

    * ``WARN`` — the estimate fell by ``PERF_WARN_DROP`` against ``baseline``. A warning only: on
      its own an estimate never forces a retrain (R4).
    * ``CRITICAL`` — the same drop **and** the realized score over the recent labelled window
      (at least ``PERF_CONFIRM_MIN_LABELS`` points) fell by as much. That is the estimated
      performance signal ADR 0022 decision 4 feeds to the cooldown-aware auto-retrain; the event
      carries ``confirmed_by_labels`` so a consumer can tell it from an unconfirmed estimate.

    The realized score is measured over the most recent ``window`` labelled points, not over the
    model's whole history, so it can actually confirm a *recent* drop.
    """
    preds = _read_recent_predictions(model, alias, window)
    values = [float(p["prediction"]) for p in preds]
    if not values:
        return {"model": model, "metric": metric, "estimated": None, "realized": None, "n": 0}

    probabilistic = all(0.0 <= v <= 1.0 for v in values)
    features = [_features_of(p) for p in preds]
    pairs = labelled_pairs(model, alias)
    used, fn, reason = resolve_perf_estimator(estimator)
    try:
        estimated, method, extra = fn(values, features, pairs, probabilistic)
    except _UNAVAILABLE as exc:
        used, reason = "builtin", _unavailable_reason(used, exc)
        estimated, method, extra = _builtin_estimate(values, features, pairs, probabilistic)
    except Exception as exc:  # noqa: BLE001 - a library failure degrades, and says so
        used, reason = "builtin", f"{used} failed: {type(exc).__name__}: {exc}"
        estimated, method, extra = _builtin_estimate(values, features, pairs, probabilistic)

    recent_pairs = pairs[-window:]
    realized = _realized(recent_pairs, probabilistic)
    n_realized = len(recent_pairs)

    drop = (baseline - estimated) if baseline is not None else 0.0
    warn = drop >= PERF_WARN_DROP
    realized_drop, confirm_basis, realized_baseline = _realized_drop(
        pairs[:-window] if len(pairs) > window else [],
        realized,
        baseline,
        probabilistic,
    )
    confirmed = bool(
        warn
        and realized_drop is not None
        and n_realized >= PERF_CONFIRM_MIN_LABELS
        and realized_drop >= PERF_WARN_DROP
    )
    severity = "CRITICAL" if confirmed else ("WARN" if warn else "OK")
    event_detail: dict[str, Any] = {
        "estimated": estimated,
        "realized": realized,
        "realized_n": n_realized,
        "baseline": baseline,
        "realized_baseline": realized_baseline,
        "confirm_basis": confirm_basis,
        "label_free": True,
        "confirmed_by_labels": confirmed,
        "estimator": used,
        "method": method,
        "warn_drop": PERF_WARN_DROP,
        **extra,
    }
    if reason:
        event_detail["estimator_fallback"] = reason
    if persist:
        platform_db.record_perf_estimate(
            model, metric, estimated=estimated, realized=realized, baseline=baseline, method=method
        )
        # An unconfirmed drop warns; a label-confirmed one is auto-retrain consumable. A recovery
        # is written too when the last estimate event was not OK — otherwise a stale confirmed
        # CRITICAL stays the newest estimate signal and `exa drift trigger` retrains forever.
        if warn or estimate_signal_open(model):
            platform_db.record_drift_event(
                model, "concept", severity=severity, score=drop, metric=metric, detail=event_detail
            )
    out: dict[str, Any] = {
        "model": model,
        "metric": metric,
        "estimated": estimated,
        "realized": realized,
        "realized_n": n_realized,
        "baseline": baseline,
        "warn": warn,
        "confirmed": confirmed,
        "severity": severity,
        "n": len(values),
        "method": method,
        "estimator": used,
        "event_detail": event_detail,
    }
    if reason:
        out["estimator_fallback"] = reason
    return out


def _features_of(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("features_json")
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def labelled_pairs(model: str, alias: str | None = None) -> list[dict[str, Any]]:
    """Prediction/label pairs in **arrival order** (oldest first), newest ``MAX_LABELLED_PAIRS``.

    The concept test splits this stream into a baseline head and a recent tail, so the order is
    the whole meaning of "recent" — the shared ``join_predictions_with_truth`` has no ORDER BY.
    Each pair carries its recorded input ``features`` (``None`` when none were logged).
    """
    sql = (
        "SELECT p.id, p.prediction, p.features_json, g.label "
        "FROM predictions p JOIN ground_truth g ON p.request_hash = g.request_hash "
        "WHERE p.model=?"
    )
    params: list[Any] = [model]
    if alias is not None:
        sql += " AND p.alias=?"
        params.append(alias)
    sql += " ORDER BY p.id DESC, g.id DESC LIMIT ?"
    params.append(MAX_LABELLED_PAIRS)
    with platform_db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    rows.reverse()
    return [
        {
            "prediction": float(r["prediction"]),
            "label": float(r["label"]),
            "features": _features_of(r),
        }
        for r in rows
        if r["prediction"] is not None and r["label"] is not None
    ]


def _read_recent_predictions(model: str, alias: str | None, limit: int) -> list[dict[str, Any]]:
    sql = "SELECT prediction, request_hash, features_json FROM predictions WHERE model=?"
    params: list[Any] = [model]
    if alias:
        sql += " AND alias=?"
        params.append(alias)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with platform_db.get_db() as conn:
        return [
            dict(r) for r in conn.execute(sql, params).fetchall() if r["prediction"] is not None
        ]


def _persist(res: DriftResult) -> None:
    platform_db.record_drift_event(
        res.model,
        res.drift_kind,
        severity=res.severity,
        score=res.score,
        metric=res.metric,
        detail=res.detail,
    )


QualityProfiler = Callable[[Sequence[dict[str, Any]]], tuple[dict[str, dict[str, Any]], int, int]]

QUALITY_PROFILER_ENV = "EXAMLOPS_DRIFT_QUALITY_PROFILER"
DEFAULT_QUALITY_PROFILER = "builtin"


def _builtin_profile(
    batch: Sequence[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """Per-field nulls / min / max / exact cardinality over the keys each row carries."""
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
    return summary, total_cells, null_cells


#: Selectable data-quality profilers. Register another with ``QUALITY_PROFILERS["name"] = fn``.
QUALITY_PROFILERS: dict[str, QualityProfiler] = {
    "builtin": _builtin_profile,
    "whylogs": adapters.whylogs_profile,
}


def resolve_quality_profiler(name: str | None = None) -> tuple[str, QualityProfiler, str | None]:
    """The profiler to run: ``(name used, fn, fallback_reason)`` — same contract as detectors."""
    want = (name or os.getenv(QUALITY_PROFILER_ENV) or DEFAULT_QUALITY_PROFILER).strip().lower()
    fn = QUALITY_PROFILERS.get(want)
    if fn is None:
        return "builtin", _builtin_profile, f"unknown profiler {want!r}"
    return want, fn, None


def profile_inference(
    model: str,
    batch: Sequence[dict[str, Any]],
    *,
    bad_payloads: int = 0,
    persist: bool = True,
    profiler: str | None = None,
) -> QualityProfile:
    """Profile an inference batch: schema / nulls / ranges / cardinality (R5).

    Records ``drift_kind=data_quality`` and folds in A5 ``bad_payloads`` — requests the inference
    ingress rejected against the contract (see
    :func:`examlops.data.drift.count_inference_rejections`) count as fully-null inputs, so a model
    whose traffic is mostly refused is not reported healthy because the few rows that got through
    were clean. A null-fraction spike escalates severity (WARN/CRITICAL). The profiler is a seam
    (``builtin`` by default, ``whylogs`` when installed).
    """
    n = len(batch)
    bad_payloads = max(int(bad_payloads), 0)
    used, fn, reason = resolve_quality_profiler(profiler)
    if n:
        try:
            summary, total_cells, null_cells = fn(batch)
        except _UNAVAILABLE as exc:
            used, reason = "builtin", _unavailable_reason(used, exc)
            summary, total_cells, null_cells = _builtin_profile(batch)
        except Exception as exc:  # noqa: BLE001 - a library failure degrades, and says so
            used, reason = "builtin", f"{used} failed: {type(exc).__name__}: {exc}"
            summary, total_cells, null_cells = _builtin_profile(batch)
    else:
        summary, total_cells, null_cells = {}, 0, 0

    denom = total_cells + bad_payloads
    null_fraction = (null_cells + bad_payloads) / denom if denom else 0.0
    if null_fraction >= QUALITY_NULL_CRITICAL:
        severity = "CRITICAL"
    elif null_fraction >= QUALITY_NULL_WARN:
        severity = "WARN"
    else:
        severity = "OK"

    prof = QualityProfile(
        model,
        n,
        summary,
        null_fraction,
        severity,
        bad_payloads=bad_payloads,
        profiler=used,
        profiler_fallback=reason,
    )
    if persist:
        detail: dict[str, Any] = {
            "n": n,
            "bad_payloads": bad_payloads,
            "fields": summary,
            "profiler": used,
        }
        if reason:
            detail["profiler_fallback"] = reason
        platform_db.record_drift_event(
            model, "data_quality", severity=severity, score=null_fraction, detail=detail
        )
    return prof


def _signal_source(event: dict[str, Any]) -> str:
    detail = event.get("detail") or {}
    return "estimated_performance" if detail.get("label_free") else "realized_error"


#: How ``record_drift_event`` serialises the label-free marker (``json.dumps`` default separators).
#: Matching it in SQL finds the newest event of each source with its own ``LIMIT 1`` — reading the
#: newest N concept events and filtering in Python would let N events of one source push the other
#: source's newest event out of the page.
_LABEL_FREE_MARK = '%"label_free": true%'


def _newest_concept_event(model: str, *, label_free: bool) -> dict[str, Any] | None:
    """The newest ``drift_kind=concept`` event of one source (estimate or realized error)."""
    cond = "detail LIKE ?" if label_free else "(detail IS NULL OR detail NOT LIKE ?)"
    platform_db.init_db()
    with platform_db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM drift_events WHERE model=? AND drift_kind='concept' AND "
            + cond
            + " ORDER BY ts DESC, id DESC LIMIT 1",
            (model, _LABEL_FREE_MARK),
        ).fetchone()
    if row is None:
        return None
    ev = dict(row)
    try:
        ev["detail"] = json.loads(ev["detail"]) if ev.get("detail") else None
    except (TypeError, ValueError):
        ev["detail"] = None
    # The LIKE is a prefilter; the parsed detail is the authority on which source wrote it.
    if (_signal_source(ev) == "estimated_performance") != label_free:
        return None
    return ev


def estimate_signal_open(model: str) -> bool:
    """Is the newest label-free estimate event for ``model`` a WARN/CRITICAL not yet cleared?

    Estimates only write a drift event when they are not OK, so without an explicit recovery event
    the last non-OK one would stay the newest estimated-performance signal indefinitely.
    """
    ev = _newest_concept_event(model, label_free=True)
    return ev is not None and ev.get("severity") != "OK"


def concept_retrain_signal(model: str) -> dict[str, Any] | None:
    """The concept-kind event auto-retrain should act on for ``model``, or ``None``.

    Two sources write ``drift_kind=concept``: the realized-error detectors and the label-free
    estimate. Reading only the newest concept event let an unconfirmed estimate WARN written after
    a realized CRITICAL hide that CRITICAL. So the newest event **per source** is taken, and the
    newest ``CRITICAL`` among them wins. An estimate may only drive a retrain when it was
    confirmed by realized labels (``confirmed_by_labels``) — an unconfirmed estimate never does,
    whatever severity a row claims (ADR 0022: "estimate only warns until confirmed").

    Returns the event plus ``signal`` = ``realized_error`` | ``estimated_performance``.
    """
    candidates = [
        (ev, source)
        for ev, source in (
            (_newest_concept_event(model, label_free=False), "realized_error"),
            (_newest_concept_event(model, label_free=True), "estimated_performance"),
        )
        if ev is not None
        and ev.get("severity") == "CRITICAL"
        and (source == "realized_error" or (ev.get("detail") or {}).get("confirmed_by_labels"))
    ]
    if candidates:
        ev, source = max(
            candidates, key=lambda c: (str(c[0].get("ts") or ""), int(c[0].get("id") or 0))
        )
        return {**ev, "signal": source}
    return None


__all__ = [
    "CONCEPT_DETECTORS",
    "PERF_ESTIMATORS",
    "QUALITY_PROFILERS",
    "DriftResult",
    "QualityProfile",
    "concept_retrain_signal",
    "detect_concept_drift",
    "estimate_signal_open",
    "estimate_performance",
    "labelled_pairs",
    "profile_inference",
    "resolve_concept_detector",
    "resolve_perf_estimator",
    "resolve_quality_profiler",
]

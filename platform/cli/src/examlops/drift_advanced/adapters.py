"""Third-party detector adapters + the pure-Python DDM for ADR 0022 (advanced drift).

ADR 0022 names four libraries — **Evidently** and **River** (ADWIN/DDM) for concept drift,
**NannyML** (CBPE/DLE) for label-free performance estimation and **whylogs** for data-quality
profiles. Each one is optional and imported lazily, inside the adapter, so the core CLI never
pays for it. River and Evidently ship as the ``examlops[drift-advanced]`` extra. NannyML (Python
< 3.13; its ``s3fs`` dependency brings fsspec/botocore bounds the ``dataplane``/``backup`` extras
exclude) and whylogs (NumPy < 2) cannot share that resolution and are installed separately. Every adapter has a
pure-Python counterpart in :mod:`examlops.drift_advanced`, and a missing or broken library degrades
to it with the reason recorded on the result — a detector that silently became a different one
would make the recorded ``detector`` / ``method`` a lie.

What each adapter asks the library, verified against the real releases (river 0.26.1,
evidently 0.7.23, nannyml 0.13.1, whylogs 1.6.4):

* ``river.drift.binary.DDM`` / ``river.drift.ADWIN`` — streaming detectors over the error stream.
* ``evidently.metrics.ValueDrift`` — a two-sample drift test (K-S p-value by default for a small
  numeric sample) of the recent realized-error distribution against the baseline one.
* ``nannyml.CBPE`` (binary probabilistic classification) and ``nannyml.DLE`` (regression) fitted on
  the labelled reference and applied to the unlabelled recent window.
* ``whylogs.log(pandas=…)`` — a column profile: counts, nulls, min/max, cardinality estimate.
  whylogs 1.6.4 references ``numpy.unicode_``, which NumPy 2 removed, so it cannot import beside
  NumPy 2; the adapter reports that as "unavailable".
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

#: DDM defaults — Gama et al. 2004, and River's ``DDM`` defaults, so both engines agree.
DDM_WARM_START = 30
DDM_WARNING_THRESHOLD = 2.0
DDM_DRIFT_THRESHOLD = 3.0
#: An error above the baseline's 75th percentile is a "failure" for DDM's Bernoulli stream. A
#: quantile of the baseline keeps the binarisation scale-free (regression errors have no natural
#: 0/1), and under no drift the failure rate stays ~25 %.
DDM_FAILURE_QUANTILE = 0.75

#: NannyML needs a labelled reference to fit on; below this it is not an estimate, it is noise.
NANNYML_MIN_REFERENCE = 50


class AdapterUnavailable(RuntimeError):
    """The optional library is absent, cannot import here, or cannot run on this data."""


def _require(module: str) -> Any:
    """Import ``module`` lazily; any import-time failure is *unavailable*, never a crash."""
    import importlib  # noqa: PLC0415

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise AdapterUnavailable(f"{module} is not installed") from exc
    except Exception as exc:  # noqa: BLE001 - e.g. whylogs on NumPy 2 raises AttributeError
        raise AdapterUnavailable(f"{module} cannot be imported here: {exc}") from exc


# -- concept drift -------------------------------------------------------------------------------


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def failure_bits(baseline: Sequence[float], recent: Sequence[float]) -> list[bool]:
    """The whole error stream as DDM's Bernoulli failures (``error > baseline p75``)."""
    threshold = _quantile(baseline, DDM_FAILURE_QUANTILE)
    return [e > threshold for e in (*baseline, *recent)]


class PureDDM:
    """Drift Detection Method (Gama, Medas, Castillo & Rodrigues, SBIA 2004), dependency-free.

    Same update rule and defaults as ``river.drift.binary.DDM`` so either engine gives the same
    answer on the same stream: track the running failure rate ``p`` and its binomial std ``s``,
    remember the minimum ``p + s``, warn when ``p + s`` exceeds ``p_min + 2·s_min`` and signal
    drift at ``p_min + 3·s_min``; after a drift the detector resets.
    """

    def __init__(
        self,
        warm_start: int = DDM_WARM_START,
        warning_threshold: float = DDM_WARNING_THRESHOLD,
        drift_threshold: float = DDM_DRIFT_THRESHOLD,
    ) -> None:
        self.warm_start = warm_start
        self.warning_threshold = warning_threshold
        self.drift_threshold = drift_threshold
        self.drift_detected = False
        self.warning_detected = False
        self._reset()

    def _reset(self) -> None:
        self._n = 0
        self._p = 0.0
        self._p_min = math.inf
        self._s_min = math.inf
        self._ps_min = math.inf

    def update(self, failed: bool) -> None:
        if self.drift_detected:
            self._reset()
            self.drift_detected = False
        self._n += 1
        self._p += (float(failed) - self._p) / self._n
        s = math.sqrt(self._p * (1 - self._p) / self._n)
        if self._n <= self.warm_start:
            return
        if self._p + s <= self._ps_min:
            self._p_min, self._s_min, self._ps_min = self._p, s, self._p + s
        self.warning_detected = self._p + s > self._p_min + self.warning_threshold * self._s_min
        if self._p + s > self._p_min + self.drift_threshold * self._s_min:
            self.drift_detected = True
            self.warning_detected = False


def scan_ddm(detector: Any, bits: Iterable[bool], first_recent: int) -> tuple[bool, bool]:
    """Feed ``bits`` through a DDM-shaped detector; ``(drift, warning)`` seen in the recent part."""
    drift = warning = False
    for i, bit in enumerate(bits):
        detector.update(bit)
        if i < first_recent:
            continue
        drift = drift or bool(detector.drift_detected)
        warning = warning or bool(getattr(detector, "warning_detected", False))
    return drift, warning


def river_ddm() -> Any:
    """A real ``river.drift.binary.DDM`` with the same thresholds as :class:`PureDDM`."""
    binary = _require("river.drift.binary")
    return binary.DDM(
        warm_start=DDM_WARM_START,
        warning_threshold=DDM_WARNING_THRESHOLD,
        drift_threshold=DDM_DRIFT_THRESHOLD,
    )


def evidently_value_drift(baseline: Sequence[float], recent: Sequence[float]) -> dict[str, Any]:
    """Evidently ``ValueDrift`` of the recent error distribution vs the baseline one.

    Returns ``{"drift": bool, "method": str, "value": float, "threshold": float}``. For a p-value
    method drift means ``value < threshold``; for a distance method ``value >= threshold`` —
    Evidently reports which one it chose in the metric's config.
    """
    evidently = _require("evidently")
    metrics = _require("evidently.metrics")
    pd = _require("pandas")
    definition = evidently.DataDefinition(numerical_columns=["error"])
    ref = evidently.Dataset.from_pandas(
        pd.DataFrame({"error": list(baseline)}), data_definition=definition
    )
    cur = evidently.Dataset.from_pandas(
        pd.DataFrame({"error": list(recent)}), data_definition=definition
    )
    snapshot = evidently.Report([metrics.ValueDrift(column="error")]).run(
        current_data=cur, reference_data=ref
    )
    try:
        metric = snapshot.dict()["metrics"][0]
        value = float(metric["value"])
        config = metric.get("config") or {}
        method = str(config.get("method") or "")
        threshold = float(config["threshold"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AdapterUnavailable(f"unexpected Evidently result shape: {exc}") from exc
    drift = value < threshold if "p_value" in method else value >= threshold
    return {"drift": drift, "method": method, "value": value, "threshold": threshold}


# -- label-free performance estimation -----------------------------------------------------------


def _numeric_feature_names(rows: Sequence[dict[str, Any] | None]) -> list[str]:
    """Feature keys that are numeric (and not bool) in *every* row — what DLE can learn from."""
    names: set[str] | None = None
    for row in rows:
        if not row:
            return []
        keys = {
            k
            for k, v in row.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        }
        names = keys if names is None else names & keys
    return sorted(names or ())


def nannyml_estimate(
    reference: Sequence[dict[str, Any]],
    analysis_predictions: Sequence[float],
    analysis_features: Sequence[dict[str, Any] | None],
    *,
    probabilistic: bool,
) -> tuple[float, str, dict[str, Any]]:
    """NannyML estimate of recent performance: ``(estimate, method, extra)``.

    ``reference`` rows carry ``prediction``, ``label`` and (for DLE) ``features``. Probabilistic
    predictions with 0/1 labels use **CBPE** (estimated accuracy); anything else uses **DLE**
    (estimated MAE, reported as ``1 / (1 + MAE)`` — the same accuracy-like proxy the realized
    regression score uses, so estimate and realized stay comparable).
    """
    if len(reference) < NANNYML_MIN_REFERENCE:
        raise AdapterUnavailable(
            f"nannyml needs >= {NANNYML_MIN_REFERENCE} labelled reference rows, got {len(reference)}"
        )
    nml = _require("nannyml")
    pd = _require("pandas")
    if probabilistic:
        labels = {int(r["label"]) for r in reference if float(r["label"]) in (0.0, 1.0)}
        if labels != {0, 1} or any(float(r["label"]) not in (0.0, 1.0) for r in reference):
            raise AdapterUnavailable("CBPE needs binary 0/1 labels with both classes present")
        ref = pd.DataFrame(
            {
                "y_pred_proba": [float(r["prediction"]) for r in reference],
                "y_pred": [int(float(r["prediction"]) >= 0.5) for r in reference],
                "y_true": [int(float(r["label"])) for r in reference],
            }
        )
        ana = pd.DataFrame(
            {
                "y_pred_proba": [float(p) for p in analysis_predictions],
                "y_pred": [int(float(p) >= 0.5) for p in analysis_predictions],
            }
        )
        est = nml.CBPE(
            y_pred_proba="y_pred_proba",
            y_pred="y_pred",
            y_true="y_true",
            problem_type="classification_binary",
            metrics=["accuracy"],
            chunk_number=1,
        )
        est.fit(ref)
        frame = est.estimate(ana).filter(period="analysis").to_df()
        value = float(frame[("accuracy", "value")].iloc[-1])
        return value, "nannyml-cbpe", {"nannyml_metric": "accuracy"}

    names = _numeric_feature_names([r.get("features") for r in reference] + list(analysis_features))
    if not names:
        raise AdapterUnavailable("DLE needs numeric input features recorded with every prediction")
    ref = pd.DataFrame(
        [
            {
                **{n: float(r["features"][n]) for n in names},
                "y_pred": float(r["prediction"]),
                "y_true": float(r["label"]),
            }
            for r in reference
        ]
    )
    ana = pd.DataFrame(
        [
            {**{n: float(f[n]) for n in names}, "y_pred": float(p)}  # type: ignore[index]
            for p, f in zip(analysis_predictions, analysis_features, strict=True)
        ]
    )
    dle = nml.DLE(
        feature_column_names=names,
        y_pred="y_pred",
        y_true="y_true",
        metrics=["mae"],
        chunk_number=1,
    )
    dle.fit(ref)
    frame = dle.estimate(ana).filter(period="analysis").to_df()
    mae = float(frame[("mae", "value")].iloc[-1])
    if not math.isfinite(mae):
        raise AdapterUnavailable("DLE returned a non-finite MAE")
    return 1.0 / (1.0 + max(mae, 0.0)), "nannyml-dle", {"nannyml_metric": "mae", "mae": mae}


# -- data-quality profiling ----------------------------------------------------------------------


def whylogs_profile(batch: Sequence[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int, int]:
    """A whylogs column profile: ``(per-field summary, total cells, null cells)``.

    The summary has the same keys as the built-in profiler (``nulls``, ``null_fraction``, ``min``,
    ``max``, ``cardinality``). One difference is deliberate and documented: whylogs profiles a
    table, so a key **absent** from a row counts as a null in that column, where the built-in
    profiler only counts the keys a row carries.
    """
    why = _require("whylogs")
    pd = _require("pandas")
    frame = pd.DataFrame(list(batch))
    view = why.log(pandas=frame).view().to_pandas()

    def _num(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    summary: dict[str, dict[str, Any]] = {}
    total = nulls_total = 0
    for column, row in view.iterrows():
        n = int(_num(row.get("counts/n")) or 0)
        nulls = int(_num(row.get("counts/null")) or 0)
        card = _num(row.get("cardinality/est"))
        summary[str(column)] = {
            "nulls": nulls,
            "null_fraction": nulls / n if n else 0.0,
            "min": _num(row.get("distribution/min")),
            "max": _num(row.get("distribution/max")),
            "cardinality": int(round(card)) if card is not None else 0,
        }
        total += n
        nulls_total += nulls
    return summary, total, nulls_total


__all__ = [
    "AdapterUnavailable",
    "PureDDM",
    "evidently_value_drift",
    "failure_bits",
    "nannyml_estimate",
    "river_ddm",
    "scan_ddm",
    "whylogs_profile",
]

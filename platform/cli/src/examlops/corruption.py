"""Corruption detection before drift diagnosis (ADR 0114 · G4.2 · G4.3).

`exa drift trigger` used to fire an automated retrain on two gates alone — a z-score
threshold and a cooldown. Silent data corruption perturbs exactly the statistic that
z-score is computed from, so a hardware fault produced a high z-score, an autonomous
retrain, and a model trained on corrupt data. Nothing in the path asked whether the
anomaly was the *data* or the *machine*.

**Why a NaN/Inf guard is not corruption detection.** Gate-level fault injection on a
production datacenter GPU (arXiv:2605.04213 — Tung, Huang, Saxena, Shirvani, Hukerikar,
Jain, Tyagi & Gongalore, *The Anatomy of Silent Data Corruption*) measured special values
(NaN/±INF) at **1.01%** of silent data corruption, and states the consequence directly:
*"Software detection targeting NaN/±INF captures minimal SDCs."* The same abstract
verifies that **single-bit flips are under 40% of bit-flip events** (so multi-bit is the
majority) and that corruption addresses exhibit periodicity. Two further shares quoted in
the ADR — nullification 50.68%, non-special flips 48.31% — are body figures and are marked
`[unverified]` there; this module does not rely on them.

**What this detector covers, and what it does not.** Per ADR 0114 R-ef, a detector may not
be credited with classes it was not tested against. :data:`DETECTOR_COVERAGE` declares the
three injected classes and which of them set ``suspected_sdc``;
:func:`measure_detection_rate` measures the rate live against injected corruption, and
``exa drift corruption selftest`` publishes it. Nullification and special values are
detected. Non-special mantissa bit-flips are **reported** through ``distribution_shift``
and deliberately do **not** set ``suspected_sdc`` — a spread change is not specific enough
to corruption to justify blocking a legitimate retrain, which is ADR 0114's named risk.

**The second axis was already being collected.** ``input_snapshots``/``input_baselines``
give embedding statistics per inference, independent of the prediction distribution:

===========================  ============  ================  ==========================
Scenario                     Input drift   Prediction drift  Correct action
===========================  ============  ================  ==========================
Real data drift              ↑             ↑                 retrain
Hardware corruption / SDC    ≈ 0           ↑                 quarantine, do not retrain
Model / serving regression   ≈ 0           ↑                 roll back the deployment
===========================  ============  ================  ==========================

Prediction drift *without* input drift is evidence **against** data drift, not for it.
:func:`classify_anomaly` reads both axes plus the corruption signal and returns the class;
the remediation is a function of the class, never of the z-score.

**`undetermined` is a first-class outcome.** When the signals do not separate — no
corruption baseline, no input evidence, or conflicting axes — the classifier says so, no
autonomous remediation fires, and an operator event is raised. Guessing here costs
GPU-hours and ships a model trained on corrupt data, so absent beats inferred (P5).
"""

from __future__ import annotations

import math
import random
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DETECTOR_COVERAGE",
    "AnomalyClassification",
    "CorruptionSignal",
    "assess_model",
    "classify_anomaly",
    "corruption_stats",
    "detect_corruption",
    "inject_mantissa_flip",
    "inject_nullification",
    "inject_special_values",
    "input_drift_rows",
    "measure_detection_rate",
    "signal_for_model",
]

# ── Thresholds (ADR 0114 §Decision) ───────────────────────────────────────────

#: Unexpected-zero rate must clear the baseline by this many standard errors.
ZERO_RATE_Z_CRIT = 3.0
#: …*and* by this much in absolute terms, so a tiny baseline cannot manufacture a huge z.
ZERO_RATE_ABS_MIN = 0.05
#: Spread inflation reported through ``distribution_shift``; never sets ``suspected_sdc``.
SPREAD_Z_REPORT = 3.0
#: Input drift at or above this counts as *moving* — the data really did change.
INPUT_ACTIVE_Z = 2.0
#: Input drift below this counts as *quiet* — the inputs did not move.
INPUT_QUIET_Z = 2.0
#: Fewer live predictions than this cannot support a corruption claim.
MIN_SAMPLES = 20
#: Fewer input snapshots than this leave the second axis unusable.
MIN_INPUT_SAMPLES = 10
#: Rows read from ``drift_snapshots`` / ``input_snapshots`` when the caller does not say.
SNAPSHOT_WINDOW = 100
INPUT_WINDOW = 200

#: Which injected corruption classes this detector was **tested** against, and whether a
#: positive detection sets ``suspected_sdc``. R-ef forbids claiming untested coverage.
DETECTOR_COVERAGE: dict[str, dict[str, Any]] = {
    "nullification": {
        "detected_by": "zero_rate vs baseline",
        "sets_suspected_sdc": True,
        "measured_share_of_sdc": "50.68% [unverified — body figure, not the abstract]",
    },
    "special_values": {
        "detected_by": "nan_inf_rate",
        "sets_suspected_sdc": True,
        "measured_share_of_sdc": "1.01% [abstract-verified]",
    },
    "mantissa_flip": {
        "detected_by": "distribution_shift (reported, not gating)",
        "sets_suspected_sdc": False,
        "measured_share_of_sdc": "48.31% [unverified — body figure, not the abstract]",
    },
}


# ── Signals ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CorruptionSignal:
    """The corruption axis for one model, over one window of predictions."""

    nan_inf_rate: float
    zero_rate: float
    zero_rate_baseline: float | None
    distribution_shift: float
    suspected_sdc: bool
    #: ``statistical_only`` while no hardware counters are available (ADR 0114 decision 6).
    evidence: str = "statistical_only"
    reasons: list[str] = field(default_factory=list)
    n: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "nan_inf_rate": self.nan_inf_rate,
            "zero_rate": self.zero_rate,
            "zero_rate_baseline": self.zero_rate_baseline,
            "distribution_shift": self.distribution_shift,
            "suspected_sdc": self.suspected_sdc,
            "evidence": self.evidence,
            "reasons": list(self.reasons),
            "n": self.n,
        }


@dataclass(frozen=True)
class AnomalyClassification:
    """What the anomaly *is*, and therefore what may be done about it."""

    #: ``data_drift`` | ``suspected_hardware`` | ``suspected_regression`` | ``undetermined``
    klass: str
    reason: str
    remediation: str
    #: Only ``data_drift`` permits an autonomous retrain (ADR 0114 decisions 2–4).
    autonomous_remediation_allowed: bool
    operator_event: bool
    evidence: str = "statistical_only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": self.klass,
            "reason": self.reason,
            "remediation": self.remediation,
            "autonomous_remediation_allowed": self.autonomous_remediation_allowed,
            "operator_event": self.operator_event,
            "evidence": self.evidence,
        }


# ── Pure statistics ───────────────────────────────────────────────────────────


def corruption_stats(values: Sequence[float]) -> dict[str, float]:
    """The statistics a corruption baseline stores. Finite values only drive mean/std.

    ``zero_rate`` counts values that are exactly zero (``-0.0`` included — nullification
    writes a zero word, and the sign bit it lands on is not evidence of anything).
    """
    n = len(values)
    if n == 0:
        return {"zero_rate": 0.0, "mean": 0.0, "std": 0.0, "n": 0.0}
    zeros = sum(1 for v in values if v == 0.0)
    finite = [float(v) for v in values if math.isfinite(v)]
    if finite:
        mean = sum(finite) / len(finite)
        std = math.sqrt(sum((v - mean) ** 2 for v in finite) / len(finite))
    else:
        mean = std = 0.0
    return {"zero_rate": zeros / n, "mean": mean, "std": std, "n": float(n)}


def _binomial_z(p_live: float, p_base: float, n_live: int, n_base: float) -> float:
    """One-sided z of an observed rate against a baseline rate.

    A baseline of exactly zero has no standard error, which would make every single
    unexpected zero look infinitely significant. It is smoothed to ``0.5/n_baseline`` (the
    rule-of-three convention for "not observed in n trials") so the z stays finite and the
    absolute-excess gate below is what carries the decision.
    """
    if n_live <= 0:
        return 0.0
    p0 = p_base if p_base > 0 else 0.5 / max(n_base, 1.0)
    p0 = min(max(p0, 1e-9), 1 - 1e-9)
    se = math.sqrt(p0 * (1.0 - p0) / n_live)
    return (p_live - p0) / se if se > 0 else 0.0


def detect_corruption(
    values: Sequence[float], baseline: dict[str, float] | None = None
) -> CorruptionSignal:
    """Corruption signal for one window of model outputs.

    A NaN/Inf guard on its own must never be described as "corruption checked" — it sees
    about 1% of the phenomenon (ADR 0114 decision 1), so this also computes the
    unexpected-zero rate against a baseline and reports a spread-shift statistic.
    """
    n = len(values)
    if n == 0:
        return CorruptionSignal(0.0, 0.0, None, 0.0, False, "no_data", ["no predictions"], 0)

    nan_inf = sum(1 for v in values if not math.isfinite(v))
    nan_inf_rate = nan_inf / n
    live = corruption_stats(values)
    zero_rate = live["zero_rate"]

    reasons: list[str] = []
    suspected = False

    if nan_inf_rate > 0:
        suspected = True
        reasons.append(f"{nan_inf} of {n} predictions are NaN/Inf")

    base_zero: float | None = None
    shift = 0.0
    if baseline is None:
        evidence = "statistical_only (no corruption baseline)"
        reasons.append("no corruption baseline — zero-rate cannot be judged")
    elif n < MIN_SAMPLES:
        base_zero = float(baseline.get("zero_rate", 0.0))
        evidence = "statistical_only (insufficient samples)"
        reasons.append(f"{n} predictions < {MIN_SAMPLES} needed to judge the zero rate")
    else:
        base_zero = float(baseline.get("zero_rate", 0.0))
        n_base = float(baseline.get("n", 0.0))
        z_zero = _binomial_z(zero_rate, base_zero, n, n_base)
        excess = zero_rate - base_zero
        if z_zero >= ZERO_RATE_Z_CRIT and excess >= ZERO_RATE_ABS_MIN:
            suspected = True
            reasons.append(
                f"unexpected-zero rate {zero_rate:.3f} vs baseline {base_zero:.3f} "
                f"(z={z_zero:.1f}, excess={excess:.3f}) — nullification pattern"
            )
        base_std = float(baseline.get("std", 0.0))
        z_std = 0.0
        if base_std > 0:
            # Spread is compared on the scale of the baseline itself: the ratio of the
            # standard deviations, expressed in the same units as the zero-rate z so one
            # `distribution_shift` number can summarise both axes.
            z_std = abs(live["std"] - base_std) / base_std * math.sqrt(max(n, 1)) / 2.0
            if z_std >= SPREAD_Z_REPORT:
                reasons.append(
                    f"spread shifted: std {live['std']:.4g} vs baseline {base_std:.4g} "
                    "— reported, not gating (mantissa-flip class is untested coverage)"
                )
        shift = max(abs(z_zero), z_std)
        evidence = "statistical_only"

    return CorruptionSignal(
        nan_inf_rate=nan_inf_rate,
        zero_rate=zero_rate,
        zero_rate_baseline=base_zero,
        distribution_shift=round(shift, 4),
        suspected_sdc=suspected,
        evidence=evidence,
        reasons=reasons,
        n=n,
    )


def classify_anomaly(
    drift_signal: dict[str, Any],
    corruption_signal: CorruptionSignal | dict[str, Any],
    input_drift: dict[str, Any] | None,
) -> AnomalyClassification:
    """Name the anomaly so the remediation follows from the class, not from the z-score.

    ``drift_signal`` needs ``z_score`` and ``status``; ``input_drift`` is a row from
    :func:`input_drift_rows` or ``None`` when the second axis has nothing to say.
    """
    corr = (
        corruption_signal
        if isinstance(corruption_signal, CorruptionSignal)
        else CorruptionSignal(
            nan_inf_rate=float(corruption_signal.get("nan_inf_rate", 0.0)),
            zero_rate=float(corruption_signal.get("zero_rate", 0.0)),
            zero_rate_baseline=corruption_signal.get("zero_rate_baseline"),
            distribution_shift=float(corruption_signal.get("distribution_shift", 0.0)),
            suspected_sdc=bool(corruption_signal.get("suspected_sdc", False)),
            evidence=str(corruption_signal.get("evidence", "statistical_only")),
            reasons=list(corruption_signal.get("reasons", [])),
            n=int(corruption_signal.get("n", 0)),
        )
    )

    # 1. A positive corruption signal outranks everything — decision 4 makes the
    #    suppression unconditional, however drift-like the rest of the picture looks.
    if corr.suspected_sdc:
        return AnomalyClassification(
            klass="suspected_hardware",
            reason="; ".join(corr.reasons) or "corruption signal positive",
            remediation="quarantine_node",
            autonomous_remediation_allowed=False,
            operator_event=True,
            evidence=corr.evidence,
        )

    status = str(drift_signal.get("status", "")).upper()
    breaching = status.startswith("CRITICAL") or status.startswith("WARNING")
    if not breaching:
        return AnomalyClassification(
            klass="undetermined",
            reason=f"prediction drift not breaching (status={drift_signal.get('status')})",
            remediation="none",
            autonomous_remediation_allowed=False,
            operator_event=False,
            evidence=corr.evidence,
        )

    # 2. Prediction drift is real. Without the second axis nothing separates data drift
    #    from a serving regression, so the honest answer is that we do not know.
    if not input_drift or int(input_drift.get("n_snapshots", 0)) < MIN_INPUT_SAMPLES:
        return AnomalyClassification(
            klass="undetermined",
            reason=(
                "prediction drift with no input-drift evidence — "
                "cannot separate data drift from a serving regression"
            ),
            remediation="none",
            autonomous_remediation_allowed=False,
            operator_event=True,
            evidence=corr.evidence,
        )
    if str(input_drift.get("status", "")).startswith("OK (no baseline)"):
        return AnomalyClassification(
            klass="undetermined",
            reason="prediction drift, but the input baseline is unset — the second axis is blind",
            remediation="none",
            autonomous_remediation_allowed=False,
            operator_event=True,
            evidence=corr.evidence,
        )

    input_z = float(input_drift.get("max_z", 0.0))

    # 3. Both axes moved: the data really did change.
    if input_z >= INPUT_ACTIVE_Z:
        return AnomalyClassification(
            klass="data_drift",
            reason=f"inputs moved too (input z={input_z:.2f}) — consistent with data drift",
            remediation="retrain",
            autonomous_remediation_allowed=True,
            operator_event=False,
            evidence=corr.evidence,
        )

    # 4. Outputs moved, inputs did not, and corruption is negative: the model or the
    #    serving path changed, so retraining would be the wrong remediation.
    if input_z < INPUT_QUIET_Z:
        return AnomalyClassification(
            klass="suspected_regression",
            reason=(
                f"predictions drifted (z={float(drift_signal.get('z_score', 0.0)):.2f}) while "
                f"inputs held (z={input_z:.2f}) and corruption is negative"
            ),
            remediation="rollback_deployment",
            autonomous_remediation_allowed=False,
            operator_event=True,
            evidence=corr.evidence,
        )

    return AnomalyClassification(
        klass="undetermined",
        reason=f"signals do not separate (input z={input_z:.2f})",
        remediation="none",
        autonomous_remediation_allowed=False,
        operator_event=True,
        evidence=corr.evidence,
    )


# ── Fault injection (R-ef: publish the rate, never assume the coverage) ────────


def inject_nullification(values: Sequence[float], rate: float, *, seed: int = 0) -> list[float]:
    """Zero a fraction of the values — the nullification class."""
    rng = random.Random(seed)
    return [0.0 if rng.random() < rate else float(v) for v in values]


def inject_special_values(values: Sequence[float], rate: float, *, seed: int = 0) -> list[float]:
    """Replace a fraction of the values with NaN/±Inf — the special-value class."""
    rng = random.Random(seed)
    specials = (float("nan"), float("inf"), float("-inf"))
    return [rng.choice(specials) if rng.random() < rate else float(v) for v in values]


def inject_mantissa_flip(
    values: Sequence[float], rate: float, *, seed: int = 0, bits: int = 3
) -> list[float]:
    """Flip low-order mantissa bits in a fraction of the values.

    The measured profile is multi-bit (single-bit flips are under 40% of events) and
    mantissa-biased, so this flips ``bits`` low mantissa bits at once. It is here to be
    *measured against*, not because the detector claims to catch it.
    """
    rng = random.Random(seed)
    out: list[float] = []
    for v in values:
        f = float(v)
        if rng.random() >= rate or not math.isfinite(f):
            out.append(f)
            continue
        (word,) = struct.unpack("<Q", struct.pack("<d", f))
        for _ in range(bits):
            word ^= 1 << rng.randrange(0, 20)  # low mantissa bits
        (flipped,) = struct.unpack("<d", struct.pack("<Q", word))
        out.append(flipped)
    return out


_INJECTORS = {
    "nullification": inject_nullification,
    "special_values": inject_special_values,
    "mantissa_flip": inject_mantissa_flip,
}


def measure_detection_rate(
    clean: Sequence[float],
    baseline: dict[str, float],
    *,
    rate: float = 0.20,
    trials: int = 20,
) -> dict[str, dict[str, Any]]:
    """Detection rate per injected corruption class, measured — never assumed (R-ef).

    Returns one entry per class in :data:`DETECTOR_COVERAGE` with the fraction of trials in
    which ``suspected_sdc`` came back true, plus the false-positive rate on the clean
    signal. A class this detector does not gate on is expected to score ~0, and reporting
    that is the point: coverage is bounded by what was injected, not by the assumed
    distribution of the phenomenon.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, injector in _INJECTORS.items():
        hits = 0
        for t in range(trials):
            corrupted = injector(clean, rate, seed=t)
            if detect_corruption(corrupted, baseline).suspected_sdc:
                hits += 1
        out[name] = {
            "injected_rate": rate,
            "trials": trials,
            "detection_rate": hits / trials if trials else 0.0,
            "gating": bool(DETECTOR_COVERAGE[name]["sets_suspected_sdc"]),
        }
    # The clean signal is deterministic, so one evaluation is the whole answer — looping
    # over it would report a rate of 0 or 1 while pretending to have sampled.
    out["_clean"] = {
        "injected_rate": 0.0,
        "trials": 1,
        "false_positive": detect_corruption(list(clean), baseline).suspected_sdc,
    }
    return out


# ── Reads over the tables that already exist ──────────────────────────────────


def input_drift_rows(model_filter: str | None = None, *, window: int = INPUT_WINDOW) -> list[dict]:
    """Input embedding drift per model — the second axis of the classification.

    It lives here rather than in the CLI because the classifier is its primary consumer;
    ``exa drift input status`` renders exactly these rows, so there is one implementation
    of the statistic rather than one per caller.
    """
    from examlops.data import get_db, init_db
    from examlops.data.drift import get_input_baseline

    init_db()
    with get_db() as conn:
        if model_filter:
            models_list = [model_filter]
        else:
            rows = conn.execute("SELECT DISTINCT model FROM input_snapshots").fetchall()
            models_list = [r["model"] for r in rows]

    results = []
    # One connection for every model, not one per model: this loop opened 101 connections for 50
    # models (a pool checkout and a round trip each on Postgres), and the rows it reads are a
    # report — nothing here needs a transaction of its own.
    with get_db() as conn:
        for model in models_list:
            snap_rows = conn.execute(
                "SELECT emb_norm, emb_mean, emb_std FROM input_snapshots WHERE model=? "
                "ORDER BY ts DESC LIMIT ?",
                (model, window),
            ).fetchall()
            if not snap_rows:
                continue
            norms = [r["emb_norm"] for r in snap_rows]
            means = [r["emb_mean"] for r in snap_rows]
            stds = [r["emb_std"] for r in snap_rows]
            live = {
                "norm_mean": sum(norms) / len(norms),
                "mean_mean": sum(means) / len(means),
                "std_mean": sum(stds) / len(stds),
            }
            baseline = get_input_baseline(model, conn=conn)
            if baseline is None:
                status = "OK (no baseline)"
                max_z = 0.0
            else:
                zs = []
                for metric in ("norm_mean", "mean_mean", "std_mean"):
                    bstd = baseline.get(f"{metric}_std", 0.0)
                    if bstd > 0:
                        zs.append(abs(live[metric] - baseline[metric]) / bstd)
                max_z = max(zs) if zs else 0.0
                if max_z >= 3.0:
                    status = "CRITICAL"
                elif max_z >= 2.0:
                    status = "WARNING"
                else:
                    status = "OK"
            results.append(
                {
                    "model": model,
                    "live_norm_mean": round(live["norm_mean"], 3),
                    "live_emb_mean": round(live["mean_mean"], 4),
                    "live_emb_std": round(live["std_mean"], 4),
                    "max_z": round(max_z, 2),
                    "status": status,
                    "n_snapshots": len(snap_rows),
                }
            )
    return results


def signal_for_model(model: str, *, window: int = SNAPSHOT_WINDOW) -> CorruptionSignal:
    """Corruption signal for a model, read from ``drift_snapshots``."""
    from examlops.data import get_db, init_db
    from examlops.data.drift import get_corruption_baseline

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT prediction FROM drift_snapshots WHERE model=? ORDER BY ts DESC, rowid DESC "
            "LIMIT ?",
            (model, window),
        ).fetchall()
    preds = [r["prediction"] for r in rows]
    return detect_corruption(preds, get_corruption_baseline(model))


def assess_model(
    model: str, drift_signal: dict[str, Any]
) -> tuple[CorruptionSignal, AnomalyClassification]:
    """Corruption signal + classification for one model, from the tables we already fill.

    ``drift_signal`` is the caller's own prediction-drift row (``z_score`` + ``status``) so
    the classifier never re-derives a number the caller has already computed and acted on.
    """
    corr = signal_for_model(model)
    rows = input_drift_rows(model)
    return corr, classify_anomaly(drift_signal, corr, rows[0] if rows else None)

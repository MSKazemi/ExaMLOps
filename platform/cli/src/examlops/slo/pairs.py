"""Paired (TTFT, TPOT) serving SLOs and their evaluator (ADR 0117 decision 2, ADR 0143 d5).

User-perceived generative latency is a *pair*: TTFT (queue + prefill) and TPOT (decode). The two
trade against each other, so one number cannot say which side is tight. A pair is::

    {ttft_ms, tpot_ms, percentile (default 99), tight: ttft|tpot, slo_class}

Semantics, stated once so every surface agrees:

* **Both dimensions must hold.** The pair is ``met`` only if the nearest-rank ``percentile`` of the
  observed TTFT is <= ``ttft_ms`` **and** that of TPOT is <= ``tpot_ms``. Exactly at a threshold
  passes (<=).
* **Attainment** is the goodput-style per-request share: requests whose TTFT *and* TPOT are both
  within their thresholds. ``burn_rate`` = (1 - attainment) / (1 - percentile/100): 1.0 means the
  error budget is consumed exactly as fast as allowed.
* **Absent is not pass.** Fewer than ``min_samples`` valid samples yields ``no_verdict``, never
  ``met`` (ADR 0117 P5). Samples that are malformed (missing/negative/NaN) are counted as
  ``rejected`` and excluded; they never silently count as good.
* ``tight`` is descriptive metadata for routing/topology policy (which side binds); it does not
  loosen the other dimension.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

TIGHT = ("ttft", "tpot")
SLO_CLASSES = ("interactive", "batch", "agent")
_MET, _VIOLATED, _NO_VERDICT = "met", "violated", "no_verdict"


def default_min_samples() -> int:
    """``EXAMLOPS_SLO_PAIR_MIN_SAMPLES`` (default 20): fewest valid samples a verdict rests on."""
    try:
        return max(1, int(os.getenv("EXAMLOPS_SLO_PAIR_MIN_SAMPLES", "20")))
    except ValueError:
        return 20


class PairSLOError(ValueError):
    """An invalid pair definition."""


@dataclass
class PairSLO:
    ttft_ms: float
    tpot_ms: float
    percentile: float = 99.0
    tight: str = "ttft"
    slo_class: str = "interactive"

    def __post_init__(self) -> None:
        for label, v in (("ttft_ms", self.ttft_ms), ("tpot_ms", self.tpot_ms)):
            if not _finite(v) or v <= 0:
                raise PairSLOError(f"{label} must be a positive number (got {v!r})")
        if not _finite(self.percentile) or not 0 < self.percentile < 100:
            raise PairSLOError(f"percentile must be in (0, 100) (got {self.percentile!r})")
        if self.tight not in TIGHT:
            raise PairSLOError(f"tight must be one of {'|'.join(TIGHT)} (got {self.tight!r})")
        if self.slo_class not in SLO_CLASSES:
            raise PairSLOError(
                f"slo_class must be one of {'|'.join(SLO_CLASSES)} (got {self.slo_class!r})"
            )


@dataclass
class PairVerdict:
    verdict: str  # met | violated | no_verdict
    n: int = 0
    rejected: int = 0
    ttft_pct_ms: float | None = None
    tpot_pct_ms: float | None = None
    ttft_ok: bool | None = None
    tpot_ok: bool | None = None
    attainment: float | None = None
    burn_rate: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Gate view: only ``met`` passes. ``no_verdict`` does not."""
        return self.verdict == _MET

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "passed": self.passed}


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation: a p99 is a latency somebody actually saw)."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _parse(sample: Any) -> tuple[float, float] | None:
    a: Any
    b: Any
    if isinstance(sample, dict):
        a, b = sample.get("ttft_ms"), sample.get("tpot_ms")
    elif isinstance(sample, (list, tuple)) and len(sample) == 2:
        a, b = sample
    else:
        return None
    if _finite(a) and _finite(b) and a >= 0 and b >= 0:
        return float(a), float(b)
    return None


def evaluate(slo: PairSLO, samples: list[Any], *, min_samples: int | None = None) -> PairVerdict:
    """Evaluate observed ``(ttft_ms, tpot_ms)`` samples (tuples or ``{ttft_ms, tpot_ms}``)."""
    need = default_min_samples() if min_samples is None else max(1, min_samples)
    parsed = [p for p in (_parse(s) for s in samples) if p is not None]
    rejected = len(samples) - len(parsed)
    if len(parsed) < need:
        return PairVerdict(
            _NO_VERDICT,
            n=len(parsed),
            rejected=rejected,
            reasons=[f"{len(parsed)} valid sample(s), need at least {need}"],
        )
    ttfts = [p[0] for p in parsed]
    tpots = [p[1] for p in parsed]
    ttft_p = percentile(ttfts, slo.percentile)
    tpot_p = percentile(tpots, slo.percentile)
    ttft_ok, tpot_ok = ttft_p <= slo.ttft_ms, tpot_p <= slo.tpot_ms
    good = sum(1 for a, b in parsed if a <= slo.ttft_ms and b <= slo.tpot_ms)
    attainment = good / len(parsed)
    budget = 1.0 - slo.percentile / 100.0
    reasons = []
    if not ttft_ok:
        reasons.append(f"TTFT p{slo.percentile:g} {ttft_p:g} ms > {slo.ttft_ms:g} ms")
    if not tpot_ok:
        reasons.append(f"TPOT p{slo.percentile:g} {tpot_p:g} ms > {slo.tpot_ms:g} ms")
    return PairVerdict(
        _MET if ttft_ok and tpot_ok else _VIOLATED,
        n=len(parsed),
        rejected=rejected,
        ttft_pct_ms=ttft_p,
        tpot_pct_ms=tpot_p,
        ttft_ok=ttft_ok,
        tpot_ok=tpot_ok,
        attainment=attainment,
        burn_rate=round((1.0 - attainment) / budget, 6),
        reasons=reasons,
    )


def set_pair(
    model: str,
    name: str,
    *,
    ttft_ms: float,
    tpot_ms: float,
    percentile: float = 99.0,
    tight: str = "ttft",
    slo_class: str = "interactive",
    tenant: str = "default",
) -> str:
    """Validate and store a pair; returns ``created`` or ``updated``. Raises :class:`PairSLOError`."""
    if not model.strip() or not name.strip():
        raise PairSLOError("model and name are required")
    PairSLO(ttft_ms, tpot_ms, percentile, tight, slo_class)  # validates
    from examlops.data import slo_pairs

    return slo_pairs.put(model, name, tenant, ttft_ms, tpot_ms, percentile, tight, slo_class)


def load_pair(model: str, name: str, tenant: str = "default") -> PairSLO | None:
    from examlops.data import slo_pairs

    row = slo_pairs.get(model, name, tenant)
    if row is None:
        return None
    return PairSLO(
        row["ttft_ms"], row["tpot_ms"], row["percentile"], row["tight"], row["slo_class"]
    )


def check_pair(
    model: str,
    name: str,
    samples: list[Any],
    *,
    tenant: str = "default",
    min_samples: int | None = None,
) -> PairVerdict:
    """Gate entry point: evaluate ``samples`` against the stored pair.

    A pair that is not declared yields ``no_verdict`` (never ``met``) with the reason.
    """
    slo = load_pair(model, name, tenant)
    if slo is None:
        return PairVerdict(_NO_VERDICT, reasons=[f"no paired SLO '{name}' declared for {model}"])
    return evaluate(slo, samples, min_samples=min_samples)

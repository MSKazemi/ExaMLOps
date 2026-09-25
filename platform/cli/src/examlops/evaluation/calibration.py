"""Judge calibration — the Minimum Viable Validation Protocol (ADR 0111, G7.1–G7.4).

An LLM judge that has never been measured is an opinion with a confidence interval of
``[0, 1]``. The largest systematic study to date (arXiv:2606.19544 — 21 judges, 9 providers,
118 runs, ~541k judgments) found that raw agreement overstates chance-corrected κ by
33.8–41.3 pp, that 11 of 21 judges shift ≥4 rank positions across benchmarks, and — the
finding this module exists for — that a judge can be almost perfectly *reproducible* and
almost perfectly *wrong*: test–retest 0.992 with position bias 0.192.

So this module measures a judge before it is allowed to gate anything:

1. **Chance-corrected agreement** — Cohen's κ with a 95 % interval, never raw agreement (G7.1).
2. **Position bias** — paired AB + BA presentation; ``|P(first position wins) − 0.5|`` (G7.2).
3. **Test–retest** — ≥3 independent replications at temperature 0, response caching off (G7.2).
4. **≥2 benchmark families** spanning preference-based and correctness-based labels (G7.2).
5. **The consistency–bias paradox** — high test–retest with high position bias is a *hard*
   failure, not a curiosity (G7.2).
6. **Rogan–Gladen correction** (arXiv:2605.06939) — a judge's raw pass-rate is an *apparent*
   prevalence measured with an imperfect instrument; correcting it by the judge's own
   sensitivity/specificity is what turns a score into a measurement (G7.4).

**Absence of calibration is not eligibility.** :func:`is_gate_eligible` returns
``(False, ["no_calibration"])`` for a judge nobody has measured — deliberately a hard
failure, because an uncalibrated judge is precisely the failure mode this exists to prevent.

The statistics here are pure functions over plain sequences: no numpy, no scipy, and no
model calls, so they are cheap to test and impossible to make flaky.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── MVVP thresholds (ADR 0111 §Decision) ──────────────────────────────────────

#: A judge whose position bias exceeds this is a coin flip in a lab coat.
POSITION_BIAS_MAX = 0.10
#: Fewer replications than this cannot separate a stable judge from a lucky one.
MIN_REPLICATIONS = 3
#: Rankings do not transfer across benchmarks — one family proves nothing.
MIN_BENCHMARK_FAMILIES = 2
#: Above this test–retest, high position bias means *reliably* biased (the paradox).
PARADOX_TEST_RETEST = 0.95
#: Label families a calibration must span.
REQUIRED_FAMILIES = ("preference", "correctness")

_Z95 = 1.959964


# ── Pure statistics ───────────────────────────────────────────────────────────


def _binarize(values: Sequence[float], threshold: float = 0.5) -> list[int]:
    return [1 if float(v) >= threshold else 0 for v in values]


def cohens_kappa(
    a: Sequence[float], b: Sequence[float], *, threshold: float = 0.5
) -> tuple[float, tuple[float, float]]:
    """Chance-corrected agreement between two binary raters, with a 95 % Wald interval.

    Returns ``(kappa, (lo, hi))``. Raw agreement is deliberately *not* returned on its own —
    reporting it is the mistake G7.1 forbids.
    """
    x, y = _binarize(a, threshold), _binarize(b, threshold)
    n = len(x)
    if n == 0 or n != len(y):
        return 0.0, (0.0, 0.0)

    po = sum(1 for i, j in zip(x, y) if i == j) / n
    pe = sum((x.count(c) / n) * (y.count(c) / n) for c in (0, 1))
    if pe >= 1.0:  # both raters constant and identical — κ is undefined, agreement is trivial
        return 0.0, (0.0, 0.0)

    kappa = (po - pe) / (1.0 - pe)
    se = math.sqrt(max(po * (1.0 - po), 0.0) / n) / (1.0 - pe)
    return kappa, (kappa - _Z95 * se, kappa + _Z95 * se)


def wilson_interval(successes: float, n: int, *, z: float = _Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion — the uncertainty G7.4 requires on every score.

    Wilson rather than Wald because eval suites are routinely small and scores routinely sit
    near 0 or 1, where the Wald interval leaves the unit interval and stops meaning anything.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = max(0.0, min(1.0, successes / n))
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def position_bias(first_position_wins: Sequence[bool]) -> float:
    """``|P(the first-presented option wins) − 0.5|`` over paired AB + BA trials.

    0.0 = order-blind. The study's worst judge scores 0.192; :data:`POSITION_BIAS_MAX` is 0.10.
    """
    trials = list(first_position_wins)
    if not trials:
        return 0.0
    return abs(sum(1 for w in trials if w) / len(trials) - 0.5)


def test_retest(replications: Sequence[Sequence[float]], *, threshold: float = 0.5) -> float:
    """Mean pairwise agreement of binarized labels across ≥2 replications of the same items.

    1.0 = perfectly reproducible. On its own this says nothing about correctness — which is
    the whole point of the paradox check.
    """
    reps = [_binarize(r, threshold) for r in replications if len(r)]
    if len(reps) < 2:
        return 0.0
    n = min(len(r) for r in reps)
    pairs = [(i, j) for i in range(len(reps)) for j in range(i + 1, len(reps))]
    agreements = [sum(1 for k in range(n) if reps[i][k] == reps[j][k]) / n for i, j in pairs if n]
    return sum(agreements) / len(agreements) if agreements else 0.0


def sensitivity_specificity(
    judge_scores: Sequence[float], human_labels: Sequence[float], *, threshold: float = 0.5
) -> tuple[float, float]:
    """The judge's own true-positive and true-negative rates against human ground truth."""
    j, h = _binarize(judge_scores, threshold), _binarize(human_labels, threshold)
    if not j or len(j) != len(h):
        return (0.0, 0.0)
    pos = [i for i, label in enumerate(h) if label == 1]
    neg = [i for i, label in enumerate(h) if label == 0]
    sens = sum(1 for i in pos if j[i] == 1) / len(pos) if pos else 0.0
    spec = sum(1 for i in neg if j[i] == 0) / len(neg) if neg else 0.0
    return (sens, spec)


def rogan_gladen(apparent: float, sensitivity: float, specificity: float) -> float | None:
    """Correct an apparent pass-rate for the judge's own error rates (arXiv:2605.06939).

    ``true = (apparent + specificity − 1) / (sensitivity + specificity − 1)``, clamped to
    ``[0, 1]``. Returns ``None`` when ``sensitivity + specificity ≤ 1`` — a judge no better
    than chance carries no information, and inventing a corrected number there would be worse
    than admitting it.
    """
    denom = sensitivity + specificity - 1.0
    if denom <= 1e-9:
        return None
    return max(0.0, min(1.0, (apparent + specificity - 1.0) / denom))


# ── Calibration record ────────────────────────────────────────────────────────


@dataclass
class CalibrationItem:
    """One labelled example. ``pair`` enables the AB+BA position-bias probe."""

    prompt: str
    human_label: float
    pair: tuple[str, str] | None = None


@dataclass
class CalibrationBenchmark:
    """A labelled benchmark. ``family`` is ``preference`` or ``correctness`` (G7.2)."""

    name: str
    family: str
    items: list[CalibrationItem] = field(default_factory=list)


@dataclass
class JudgeCalibration:
    judge: str
    version: str
    at: str
    kappa: float
    kappa_ci: tuple[float, float]
    position_bias: float
    test_retest: float
    benchmarks: list[str]
    families: list[str]
    replications: int
    paradox_flag: bool
    sensitivity: float
    specificity: float
    n: int
    calibration_id: str = ""

    def __post_init__(self) -> None:
        if not self.calibration_id:
            self.calibration_id = self._derive_id()

    def _derive_id(self) -> str:
        basis = json.dumps(
            {
                "judge": self.judge,
                "version": self.version,
                "at": self.at,
                "kappa": round(self.kappa, 6),
                "position_bias": round(self.position_bias, 6),
                "test_retest": round(self.test_retest, 6),
                "benchmarks": sorted(self.benchmarks),
            },
            sort_keys=True,
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        d = vars(self).copy()
        d["kappa_ci"] = list(self.kappa_ci)
        return d


def eligibility_failures(cal: JudgeCalibration) -> list[str]:
    """Every MVVP check this calibration fails, named. Empty list = gate-eligible."""
    failed: list[str] = []
    if cal.position_bias > POSITION_BIAS_MAX:
        failed.append(f"position_bias {cal.position_bias:.3f} > {POSITION_BIAS_MAX}")
    if cal.replications < MIN_REPLICATIONS:
        failed.append(f"replications {cal.replications} < {MIN_REPLICATIONS}")
    families = {f for f in cal.families}
    if len(families) < MIN_BENCHMARK_FAMILIES:
        failed.append(f"benchmark_families {len(families)} < {MIN_BENCHMARK_FAMILIES}")
    elif not set(REQUIRED_FAMILIES).issubset(families):
        missing = sorted(set(REQUIRED_FAMILIES) - families)
        failed.append(f"missing benchmark family: {', '.join(missing)}")
    if cal.paradox_flag:
        failed.append(
            f"consistency-bias paradox (test_retest {cal.test_retest:.3f} > "
            f"{PARADOX_TEST_RETEST} with position_bias {cal.position_bias:.3f})"
        )
    lo, hi = cal.kappa_ci
    if lo == 0.0 and hi == 0.0:
        failed.append("kappa reported without an interval")
    return failed


def calibrate(
    judge_fn: Callable[[str], float],
    benchmarks: Sequence[CalibrationBenchmark],
    *,
    judge: str = "judge",
    version: str = "v1",
    replications: int = MIN_REPLICATIONS,
    at: str | None = None,
) -> JudgeCalibration:
    """Run the MVVP against ``judge_fn`` and return the measurement.

    ``judge_fn`` is the same seam :class:`~examlops.evaluation.LLMJudge` uses, and is expected
    to be called at temperature 0 with response caching **off** — replications that hit a cache
    measure the cache, not the judge. This function never silently repairs a bad protocol: it
    records ``replications`` as run, and :func:`eligibility_failures` refuses fewer than three.
    """
    at = at or datetime.now(UTC).isoformat(timespec="seconds")
    # ADR 0007 decision 4: every judge call goes through the harness's temperature-0 invoker, so a
    # seam that takes ``temperature`` is *set* to 0 and one declaring anything else is refused.
    from examlops.evaluation import invoke_judge

    def call(prompt: str) -> float:
        return float(invoke_judge(judge_fn, prompt)[0])

    all_human: list[float] = []
    per_replication: list[list[float]] = [[] for _ in range(max(1, replications))]
    first_position_wins: list[bool] = []
    names: list[str] = []
    families: list[str] = []

    for bench in benchmarks:
        names.append(bench.name)
        families.append(bench.family)
        for item in bench.items:
            all_human.append(item.human_label)
            for r in range(max(1, replications)):
                per_replication[r].append(call(item.prompt))
            if item.pair is not None:
                a, b = item.pair
                ab = call(f"{item.prompt}\n[A]\n{a}\n[B]\n{b}")
                ba = call(f"{item.prompt}\n[A]\n{b}\n[B]\n{a}")
                # In each presentation the *first* slot holds a different candidate; a
                # order-blind judge picks the same candidate both times, so exactly one of
                # the two trials is a "first position won".
                first_position_wins.append(ab >= 0.5)
                first_position_wins.append(ba >= 0.5)

    primary = per_replication[0]
    kappa, ci = cohens_kappa(primary, all_human)
    sens, spec = sensitivity_specificity(primary, all_human)
    retest = test_retest(per_replication)
    bias = position_bias(first_position_wins)

    return JudgeCalibration(
        judge=judge,
        version=version,
        at=at,
        kappa=kappa,
        kappa_ci=ci,
        position_bias=bias,
        test_retest=retest,
        benchmarks=names,
        families=sorted(set(families)),
        replications=max(1, replications),
        paradox_flag=retest > PARADOX_TEST_RETEST and bias > POSITION_BIAS_MAX,
        sensitivity=sens,
        specificity=spec,
        n=len(all_human),
    )


def calibrate_from_records(
    records: dict[str, Any], *, judge: str, version: str = "v1", at: str | None = None
) -> JudgeCalibration:
    """Compute the MVVP from *already collected* judgments — the offline path the CLI uses.

    Calibration needs a labelled benchmark and several replications; collecting those is a
    batch job, not something to re-run inside a CLI invocation. This takes the collected
    judgments and does only the arithmetic, so the measurement is reproducible from a file
    that can be committed and reviewed::

        {"benchmarks": [{"name": "mt-bench-sample", "family": "preference",
                         "items": [{"human_label": 1,
                                    "judge_scores": [1, 1, 1],       # one per replication
                                    "ab_first_wins": [true, false]}  # optional AB+BA outcomes
                                  ]}]}

    ``replications`` is inferred as the *minimum* number of judge scores on any item — an item
    scored once cannot support a three-replication claim, and taking the maximum would let one
    well-sampled item vouch for the rest.
    """
    at = at or datetime.now(UTC).isoformat(timespec="seconds")
    benches = records.get("benchmarks") or []

    all_human: list[float] = []
    reps_count = None
    per_replication: list[list[float]] = []
    first_position_wins: list[bool] = []
    names: list[str] = []
    families: list[str] = []

    for bench in benches:
        names.append(str(bench.get("name", "unnamed")))
        families.append(str(bench.get("family", "unknown")))
        for item in bench.get("items") or []:
            scores = [float(s) for s in (item.get("judge_scores") or [])]
            if not scores:
                continue
            reps_count = len(scores) if reps_count is None else min(reps_count, len(scores))
            all_human.append(float(item.get("human_label", 0.0)))
            for r, s in enumerate(scores):
                while len(per_replication) <= r:
                    per_replication.append([])
                per_replication[r].append(s)
            first_position_wins.extend(bool(w) for w in (item.get("ab_first_wins") or []))

    primary = per_replication[0] if per_replication else []
    kappa, ci = cohens_kappa(primary, all_human)
    sens, spec = sensitivity_specificity(primary, all_human)
    retest = test_retest(per_replication)
    bias = position_bias(first_position_wins)

    return JudgeCalibration(
        judge=judge,
        version=version,
        at=at,
        kappa=kappa,
        kappa_ci=ci,
        position_bias=bias,
        test_retest=retest,
        benchmarks=names,
        families=sorted(set(families)),
        replications=reps_count or 0,
        paradox_flag=retest > PARADOX_TEST_RETEST and bias > POSITION_BIAS_MAX,
        sensitivity=sens,
        specificity=spec,
        n=len(all_human),
    )


# ── Eligibility, resolved against what is actually recorded ───────────────────


def is_gate_eligible(judge: str, *, version: str | None = None) -> tuple[bool, list[str]]:
    """``(eligible, failed_checks)`` for the judge's latest recorded calibration.

    A judge nobody has measured returns ``(False, ["no_calibration"])`` — **absence of
    calibration is not eligibility** (ADR 0111 §Decision 7).
    """
    if not judge:
        return (False, ["no_calibration"])
    from examlops.data.evaluation import get_judge_calibration

    row = get_judge_calibration(judge, version=version)
    if row is None:
        return (False, ["no_calibration"])
    cal = calibration_from_row(row)
    failures = eligibility_failures(cal)
    return (not failures, failures)


def calibration_from_row(row: dict[str, Any]) -> JudgeCalibration:
    """Rehydrate a stored row into a :class:`JudgeCalibration`."""
    return JudgeCalibration(
        judge=row["judge"],
        version=row.get("version") or "v1",
        at=row.get("at") or "",
        kappa=float(row.get("kappa") or 0.0),
        kappa_ci=(float(row.get("kappa_lo") or 0.0), float(row.get("kappa_hi") or 0.0)),
        position_bias=float(row.get("position_bias") or 0.0),
        test_retest=float(row.get("test_retest") or 0.0),
        benchmarks=json.loads(row.get("benchmarks") or "[]"),
        families=json.loads(row.get("families") or "[]"),
        replications=int(row.get("replications") or 0),
        paradox_flag=bool(row.get("paradox_flag")),
        sensitivity=float(row.get("sensitivity") or 0.0),
        specificity=float(row.get("specificity") or 0.0),
        n=int(row.get("n") or 0),
        calibration_id=row.get("calibration_id") or "",
    )

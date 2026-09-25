"""Statistically-rigorous A/B comparison (#13).

Replaces the manual bookkeeping-only A/B path with real hypothesis tests:

* :func:`welch_t_test` — Welch's unequal-variance t-test for continuous metrics
  (RMSE, latency, accuracy scores).
* :func:`two_proportion_z_test` — for binary outcomes (success/failure rates).
* :func:`analyze_ab` — the high-level decision: enforces a minimum sample size,
  runs the appropriate test, and reports significance + the winning variant given
  the metric's optimisation direction.

Only depends on numpy + scipy.stats (already in the environment), so it stays a
pure, side-effect-free module usable by the CLI, canary analysis (#11) and eval (#14).
"""

from __future__ import annotations

import math
from typing import Any


def welch_t_test(a: list[float], b: list[float]) -> dict[str, Any]:
    """Two-sided Welch's t-test (does not assume equal variance).

    Returns t-statistic, Welch–Satterthwaite degrees of freedom, two-sided p-value,
    and per-group means/sizes. Raises ``ValueError`` if either group has < 2 points.
    """
    import numpy as np
    from scipy import stats

    xa = np.asarray(a, dtype=float)
    xb = np.asarray(b, dtype=float)
    na, nb = xa.size, xb.size
    if na < 2 or nb < 2:
        raise ValueError("Welch's t-test needs at least 2 observations per variant")

    mean_a, mean_b = float(xa.mean()), float(xb.mean())
    var_a, var_b = float(xa.var(ddof=1)), float(xb.var(ddof=1))
    se = math.sqrt(var_a / na + var_b / nb)
    if se == 0.0:
        # Identical, zero-variance groups → no detectable difference.
        return {
            "test": "welch_t",
            "t_stat": 0.0,
            "df": float(na + nb - 2),
            "p_value": 1.0,
            "mean_a": mean_a,
            "mean_b": mean_b,
            "n_a": na,
            "n_b": nb,
        }

    t_stat = (mean_a - mean_b) / se
    df_num = (var_a / na + var_b / nb) ** 2
    df_den = (var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1)
    df = df_num / df_den
    p_value = float(2.0 * stats.t.sf(abs(t_stat), df))
    return {
        "test": "welch_t",
        "t_stat": float(t_stat),
        "df": float(df),
        "p_value": p_value,
        "mean_a": mean_a,
        "mean_b": mean_b,
        "n_a": na,
        "n_b": nb,
    }


def two_proportion_z_test(successes_a: int, n_a: int, successes_b: int, n_b: int) -> dict[str, Any]:
    """Two-sided two-proportion z-test for binary outcomes."""
    from scipy import stats

    if n_a <= 0 or n_b <= 0:
        raise ValueError("two-proportion z-test needs non-empty groups")
    rate_a = successes_a / n_a
    rate_b = successes_b / n_b
    pooled = (successes_a + successes_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0.0:
        return {
            "test": "two_proportion_z",
            "z_stat": 0.0,
            "p_value": 1.0,
            "rate_a": rate_a,
            "rate_b": rate_b,
            "n_a": n_a,
            "n_b": n_b,
        }
    z = (rate_a - rate_b) / se
    p_value = float(2.0 * stats.norm.sf(abs(z)))
    return {
        "test": "two_proportion_z",
        "z_stat": float(z),
        "p_value": p_value,
        "rate_a": rate_a,
        "rate_b": rate_b,
        "n_a": n_a,
        "n_b": n_b,
    }


def analyze_ab(
    values_a: list[float],
    values_b: list[float],
    *,
    lower_is_better: bool = False,
    alpha: float = 0.05,
    min_sample: int = 30,
) -> dict[str, Any]:
    """Decide an A/B test outcome from two samples of a continuous metric.

    ``lower_is_better`` flips the winner interpretation (e.g. RMSE / latency).
    A verdict of ``"insufficient_sample"`` is returned until both variants reach
    ``min_sample`` observations, so we never call a winner on noise.
    """
    n_a, n_b = len(values_a), len(values_b)
    if n_a < min_sample or n_b < min_sample:
        return {
            "verdict": "insufficient_sample",
            "significant": False,
            "winner": None,
            "n_a": n_a,
            "n_b": n_b,
            "min_sample": min_sample,
        }

    result = welch_t_test(values_a, values_b)
    significant = result["p_value"] < alpha

    winner: str | None = None
    if significant:
        a_beats_b = (
            result["mean_a"] < result["mean_b"]
            if lower_is_better
            else result["mean_a"] > result["mean_b"]
        )
        winner = "a" if a_beats_b else "b"

    return {
        "verdict": "significant" if significant else "no_difference",
        "significant": significant,
        "winner": winner,
        "alpha": alpha,
        "lower_is_better": lower_is_better,
        **result,
    }


# ── non-inferiority (ADR 0146 decisions 3 and 4) ─────────────────────────────────────────────
# "Is the candidate no worse than the incumbent by more than a declared margin?" is a different
# question from "is there a difference?", and the two-sided tests above cannot answer it: a
# candidate 0.5 points worse on 40 samples is "no_difference" to Welch, and that verdict would
# wave through exactly the regression a margin exists to stop. The decision here is the
# one-sided confidence bound on the difference, compared with the margin; the measured
# difference is always reported beside the verdict (ADR 0117: record the number, not just
# pass/fail), so a margin that has become a rubber stamp is visible in the record.


def _wilson(successes: float, n: int, z: float) -> tuple[float, float]:
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


def non_inferiority_proportions(
    successes_candidate: int,
    n_candidate: int,
    successes_baseline: int,
    n_baseline: int,
    *,
    margin: float,
    alpha: float = 0.05,
    higher_is_better: bool = True,
) -> dict[str, Any]:
    """One-sided non-inferiority test for two proportions (Newcombe hybrid score interval).

    The difference is ``candidate - baseline``. With ``higher_is_better`` the candidate is
    non-inferior when the lower one-sided ``1 - alpha`` bound of the difference is above
    ``-margin``; for a rate where lower is better (an unsafe-answer rate), when the upper bound
    is below ``+margin``. Newcombe's method 10 is used rather than the Wald interval because the
    Wald interval collapses to zero width at 0 % and 100 % - exactly where safety metrics live.
    Raises ``ValueError`` on an empty group, a count outside ``[0, n]`` or a bad margin/alpha.
    """
    from scipy import stats

    if n_candidate <= 0 or n_baseline <= 0:
        raise ValueError("non-inferiority needs a non-empty sample on both sides")
    if not (0 <= successes_candidate <= n_candidate and 0 <= successes_baseline <= n_baseline):
        raise ValueError("successes must lie in [0, n]")
    if not (math.isfinite(margin) and margin >= 0):
        raise ValueError("margin must be a finite number >= 0")
    if not 0 < alpha < 0.5:
        raise ValueError("alpha must be in (0, 0.5)")
    z = float(stats.norm.ppf(1 - alpha))
    p1 = successes_candidate / n_candidate
    p2 = successes_baseline / n_baseline
    l1, u1 = _wilson(successes_candidate, n_candidate, z)
    l2, u2 = _wilson(successes_baseline, n_baseline, z)
    diff = p1 - p2
    lower = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    ok = lower > -margin if higher_is_better else upper < margin
    return {
        "test": "non_inferiority_newcombe",
        "non_inferior": bool(ok),
        "difference": diff,
        "lower": lower,
        "upper": upper,
        "margin": margin,
        "alpha": alpha,
        "higher_is_better": higher_is_better,
        "rate_candidate": p1,
        "rate_baseline": p2,
        "n_candidate": n_candidate,
        "n_baseline": n_baseline,
    }


def non_inferiority_welch(
    candidate: list[float],
    baseline: list[float],
    *,
    margin: float,
    alpha: float = 0.05,
    higher_is_better: bool = True,
) -> dict[str, Any]:
    """One-sided Welch non-inferiority test for a continuous metric (per-sample values).

    H0: the candidate is worse than the baseline by at least ``margin`` in the metric's own
    direction; rejected (non-inferior) when ``p_value < alpha``.
    """
    import numpy as np
    from scipy import stats

    if not (math.isfinite(margin) and margin >= 0):
        raise ValueError("margin must be a finite number >= 0")
    xc = np.asarray(candidate, dtype=float)
    xb = np.asarray(baseline, dtype=float)
    nc, nb = xc.size, xb.size
    if nc < 2 or nb < 2:
        raise ValueError("non-inferiority needs at least 2 observations per group")
    mc, mb = float(xc.mean()), float(xb.mean())
    vc, vb = float(xc.var(ddof=1)), float(xb.var(ddof=1))
    se = math.sqrt(vc / nc + vb / nb)
    gain = (mc - mb) if higher_is_better else (mb - mc)  # > 0: candidate better
    if se == 0.0:
        p_value = 0.0 if gain + margin > 0 else 1.0
        df = float(nc + nb - 2)
        t_stat = math.inf if gain + margin > 0 else -math.inf
    else:
        t_stat = (gain + margin) / se
        df = (vc / nc + vb / nb) ** 2 / ((vc / nc) ** 2 / (nc - 1) + (vb / nb) ** 2 / (nb - 1))
        p_value = float(stats.t.sf(t_stat, df))
    return {
        "test": "non_inferiority_welch",
        "non_inferior": bool(p_value < alpha),
        "difference": mc - mb,
        "t_stat": float(t_stat),
        "df": float(df),
        "p_value": float(p_value),
        "margin": margin,
        "alpha": alpha,
        "higher_is_better": higher_is_better,
        "mean_candidate": mc,
        "mean_baseline": mb,
        "n_candidate": nc,
        "n_baseline": nb,
    }

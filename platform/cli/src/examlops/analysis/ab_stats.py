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

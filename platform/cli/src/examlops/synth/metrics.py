"""A7 — fidelity & privacy metrics (ADR 0042, spec R2/R3).

Both families are computed in pure numpy so the release gate works even in the SDV-less
fallback path (spec R3: a memorizing generator MUST be flagged and blocked — the gate can
never be a silent no-op). Scores are normalised to ``[0, 1]`` where **higher is better**:

* **fidelity** — 1.0 means the synthetic marginals + correlations match the real data.
* **privacy**  — 1.0 means synthetic rows are as far from the real rows as real rows are
  from each other; 0.0 means the generator memorised/copied real records.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_EPS = 1e-9


def _shared_columns(real: pd.DataFrame, synth: pd.DataFrame) -> list[str]:
    return [c for c in real.columns if c in synth.columns]


def _numeric_cols(df: pd.DataFrame, cols: list[str]) -> list[str]:
    return [
        c
        for c in cols
        if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
    ]


def _is_scalar_column(series: pd.Series) -> bool:
    """False for columns holding lists/arrays/dicts (e.g. embeddings) — not comparable
    by ``value_counts``; such columns are skipped in the distribution fidelity term."""
    for v in series.dropna().head(32):
        if isinstance(v, (list, tuple, dict, np.ndarray)):
            return False
    return True


def _ks_statistic(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sample Kolmogorov–Smirnov statistic in [0, 1] (0 = identical distributions)."""
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    if a.size == 0 or b.size == 0:
        return 1.0
    grid = np.sort(np.concatenate([a, b]))
    cdf_a = np.searchsorted(np.sort(a), grid, side="right") / a.size
    cdf_b = np.searchsorted(np.sort(b), grid, side="right") / b.size
    return float(np.max(np.abs(cdf_a - cdf_b)))


def _tv_distance(real: pd.Series, synth: pd.Series) -> float:
    """Total-variation distance between two categorical distributions in [0, 1]."""
    rp = real.value_counts(normalize=True, dropna=False)
    sp = synth.value_counts(normalize=True, dropna=False)
    categories = set(rp.index) | set(sp.index)
    return 0.5 * float(sum(abs(rp.get(k, 0.0) - sp.get(k, 0.0)) for k in categories))


def fidelity_metrics(real: pd.DataFrame, synth: pd.DataFrame) -> dict:
    """Per-column distribution fidelity + correlation preservation (spec R2).

    ``score`` in [0, 1] (higher = better). Column scores are ``1 - KS`` (numeric) or
    ``1 - TV`` (categorical); a correlation-structure term folds in the mean absolute
    delta between the real and synthetic numeric correlation matrices.
    """
    cols = _shared_columns(real, synth)
    if not cols:
        return {"score": 0.0, "columns": {}, "correlation_delta": None}

    column_scores: dict[str, float] = {}
    num = _numeric_cols(real, cols)
    for c in cols:
        if c in num:
            ks = _ks_statistic(real[c].to_numpy(dtype=float), synth[c].to_numpy(dtype=float))
            column_scores[c] = 1.0 - ks
        elif _is_scalar_column(real[c]):
            column_scores[c] = 1.0 - _tv_distance(real[c], synth[c])
        # list/embedding columns are not distribution-comparable here — skipped (spec:
        # fidelity is over the tabular marginals; embedding drift is A7's non-goal → C5/B6).

    if not column_scores:
        return {"score": 0.0, "columns": {}, "correlation_delta": None}

    corr_delta: float | None = None
    corr_score = 1.0
    if len(num) >= 2:
        with np.errstate(invalid="ignore"):
            cr = np.nan_to_num(np.corrcoef(real[num].to_numpy(dtype=float), rowvar=False))
            cs = np.nan_to_num(np.corrcoef(synth[num].to_numpy(dtype=float), rowvar=False))
        iu = np.triu_indices(len(num), k=1)
        corr_delta = float(np.mean(np.abs(cr[iu] - cs[iu])))
        corr_score = max(0.0, 1.0 - corr_delta / 2.0)  # corr∈[-1,1] ⇒ delta∈[0,2]

    marginal = float(np.mean(list(column_scores.values())))
    # Weight marginals more heavily than the correlation term.
    score = marginal if corr_delta is None else 0.75 * marginal + 0.25 * corr_score
    return {
        "score": round(max(0.0, min(1.0, score)), 6),
        "columns": {k: round(v, 6) for k, v in column_scores.items()},
        "correlation_delta": None if corr_delta is None else round(corr_delta, 6),
    }


def _standardized(
    df: pd.DataFrame, num: list[str], mean: np.ndarray, std: np.ndarray
) -> np.ndarray:
    x = df[num].to_numpy(dtype=float)
    x = np.nan_to_num(x, nan=0.0)
    return (x - mean) / std


def privacy_metrics(real: pd.DataFrame, synth: pd.DataFrame) -> dict:
    """Distance-to-closest-record + a membership-inference proxy (spec R2/R3).

    ``score`` in [0, 1] (higher = safer). Combines:

    * **DCR ratio** — median nearest-real-neighbour distance of the synthetic rows,
      divided by the typical real-to-real nearest-neighbour distance. ~1 ⇒ synthetic rows
      are no closer to real rows than real rows are to each other (safe); ~0 ⇒ memorised.
    * **exact-match fraction** — share of synthetic rows that duplicate a real record
      across all shared columns (a hard memorisation signal); the score is capped by
      ``1 - exact_match_fraction`` so copying real rows can never pass.
    """
    cols = _shared_columns(real, synth)
    if not cols or real.empty or synth.empty:
        return {"score": 0.0, "dcr_median": None, "baseline": None, "exact_match_fraction": 1.0}

    # Exact-duplicate detection over all shared columns (stringified for list/embedding safety).
    real_keys = {tuple(map(_hashable, row)) for row in real[cols].to_numpy()}
    synth_rows = [tuple(map(_hashable, row)) for row in synth[cols].to_numpy()]
    exact_matches = sum(1 for r in synth_rows if r in real_keys)
    exact_fraction = exact_matches / len(synth_rows)

    num = _numeric_cols(real, cols)
    if not num:
        # No numeric geometry: rely solely on exact-match memorisation signal.
        score = 1.0 - exact_fraction
        return {
            "score": round(max(0.0, min(1.0, score)), 6),
            "dcr_median": None,
            "baseline": None,
            "exact_match_fraction": round(exact_fraction, 6),
        }

    mean = np.nan_to_num(real[num].to_numpy(dtype=float)).mean(axis=0)
    std = np.nan_to_num(real[num].to_numpy(dtype=float)).std(axis=0)
    std = np.where(std < _EPS, 1.0, std)
    r = _standardized(real, num, mean, std)
    s = _standardized(synth, num, mean, std)

    synth_dcr = _nearest_distances(s, r)
    baseline = _real_baseline(r)
    dcr_median = float(np.median(synth_dcr))
    ratio = dcr_median / (baseline + _EPS)
    ratio_score = float(np.clip(ratio, 0.0, 1.0))
    score = min(ratio_score, 1.0 - exact_fraction)
    return {
        "score": round(max(0.0, min(1.0, score)), 6),
        "dcr_median": round(dcr_median, 6),
        "baseline": round(baseline, 6),
        "exact_match_fraction": round(exact_fraction, 6),
    }


def _hashable(v: object) -> object:
    if isinstance(v, (list, tuple, np.ndarray)):
        return tuple(np.asarray(v).ravel().tolist())
    return v


def _nearest_distances(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """For each row of ``a``, the Euclidean distance to its nearest row in ``b``."""
    out = np.empty(a.shape[0])
    for i in range(a.shape[0]):
        d = np.sqrt(np.sum((b - a[i]) ** 2, axis=1))
        out[i] = np.min(d) if d.size else 0.0
    return out


def _real_baseline(r: np.ndarray) -> float:
    """Median real-to-real nearest-neighbour distance (excluding self)."""
    if r.shape[0] < 2:
        return 1.0
    dists = np.empty(r.shape[0])
    for i in range(r.shape[0]):
        d = np.sqrt(np.sum((r - r[i]) ** 2, axis=1))
        d[i] = np.inf
        dists[i] = np.min(d)
    finite = dists[np.isfinite(dists)]
    base = float(np.median(finite)) if finite.size else 1.0
    return base if base > _EPS else 1.0

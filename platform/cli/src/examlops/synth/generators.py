"""A7 — synthetic data generators (ADR 0042, spec ``A7-synthetic-data-generation``).

Two interchangeable backends behind one :class:`Synthesizer` value object:

* **SDV** (preferred) — when the optional ``examlops[synth]`` extra is installed, a
  Gaussian-Copula / CTGAN / TVAE synthesizer from the `sdv` library is fitted.
* **pure-python fallback** — otherwise a dependency-free Gaussian-copula sampler
  (empirical marginals + a rank-correlation copula for numeric columns, empirical
  frequencies for categoricals, bootstrap resampling for anything else). This keeps the
  feature fully functional offline (laptop, CI, tests) with no heavy ML dependency —
  the same graceful-degradation pattern as A1 (lakeFS→content-hash).

The fallback is deliberate and load-bearing: fidelity/privacy gating (see ``metrics``)
must run on its output too, so the release gate is never a silent no-op.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = logging.getLogger(__name__)

#: Generator methods this module understands. SDV realises them natively; the fallback
#: approximates all three with the same Gaussian-copula sampler (logged when it does).
METHODS = ("gaussian_copula", "ctgan", "tvae")

_EPS = 1e-9


def _normal_cdf(z: np.ndarray) -> np.ndarray:
    """Standard-normal CDF Φ, vectorised over ``z`` (stdlib only, no scipy dependency)."""
    nd = statistics.NormalDist()
    return np.array([nd.cdf(float(v)) for v in np.asarray(z).ravel()]).reshape(np.shape(z))


def _normal_ppf(u: np.ndarray) -> np.ndarray:
    """Standard-normal inverse-CDF Φ⁻¹, vectorised (stdlib ``NormalDist`` — no scipy)."""
    nd = statistics.NormalDist()
    clipped = np.clip(np.asarray(u, dtype=float), _EPS, 1.0 - _EPS)
    return np.array([nd.inv_cdf(float(v)) for v in clipped.ravel()]).reshape(clipped.shape)


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)


def _is_categorical(series: pd.Series) -> bool:
    return (
        pd.api.types.is_bool_dtype(series)
        or isinstance(series.dtype, pd.CategoricalDtype)
        or pd.api.types.is_object_dtype(series)
        or pd.api.types.is_string_dtype(series)
    )


def _scalar_categorical(series: pd.Series) -> bool:
    """True only when object values are hashable scalars (not lists/arrays/dicts)."""
    for v in series.dropna().head(32):
        if isinstance(v, (list, tuple, dict, np.ndarray)):
            return False
    return True


@dataclass
class Synthesizer:
    """A fitted generator plus the metadata needed to reproduce its schema (spec R1)."""

    method: str
    source_revision: str
    backend: str  # "sdv" | "fallback"
    columns: list[str]
    dtypes: dict[str, str]
    numeric_cols: list[str] = field(default_factory=list)
    categorical_cols: list[str] = field(default_factory=list)
    other_cols: list[str] = field(default_factory=list)
    # fallback state
    _quantiles: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    _corr: np.ndarray | None = field(default=None, repr=False)
    _cat_dist: dict[str, tuple[list[Any], np.ndarray]] = field(default_factory=dict, repr=False)
    _bootstrap: dict[str, list[Any]] = field(default_factory=dict, repr=False)
    _sdv_model: Any = field(default=None, repr=False)
    seed: int = 0

    @property
    def params(self) -> dict[str, Any]:
        """Serialisable generator config recorded as A2 provenance (spec R4)."""
        return {
            "method": self.method,
            "backend": self.backend,
            "source_revision": self.source_revision,
            "n_columns": len(self.columns),
            "numeric": self.numeric_cols,
            "categorical": self.categorical_cols,
            "other": self.other_cols,
            "seed": self.seed,
        }


def _fit_fallback(synth: Synthesizer, data: pd.DataFrame) -> None:
    """Fit the dependency-free Gaussian-copula sampler (numeric) + empirical marginals."""
    # Numeric: store empirical values (for inverse-quantile) + a rank-correlation matrix.
    num = synth.numeric_cols
    if num:
        z_cols = []
        for col in num:
            values = data[col].to_numpy(dtype=float)
            values = values[~np.isnan(values)]
            if values.size == 0:
                values = np.zeros(1)
            synth._quantiles[col] = np.sort(values)
            # Rank → uniform → normal scores (Gaussian copula transform).
            ranks = pd.Series(data[col]).rank(method="average").to_numpy(dtype=float)
            u = ranks / (len(ranks) + 1.0)
            z_cols.append(_normal_ppf(u))
        z = np.column_stack(z_cols)
        # Correlation of the normal scores; guard tiny/constant samples.
        if z.shape[0] > 1 and z.shape[1] >= 1:
            with np.errstate(invalid="ignore"):
                corr = np.corrcoef(z, rowvar=False)
            corr = np.atleast_2d(corr)
            corr = np.nan_to_num(corr, nan=0.0)
            np.fill_diagonal(corr, 1.0)
        else:
            corr = np.eye(len(num))
        synth._corr = corr
    # Categorical: empirical frequency distribution.
    for col in synth.categorical_cols:
        counts = data[col].value_counts(dropna=False)
        cat_values: list[Any] = list(counts.index)
        cat_probs: np.ndarray = counts.to_numpy(dtype=float)
        cat_probs = cat_probs / cat_probs.sum()
        synth._cat_dist[col] = (cat_values, cat_probs)
    # Other (lists/embeddings/unhashables): bootstrap resample preserves the marginal.
    for col in synth.other_cols:
        synth._bootstrap[col] = list(data[col])


def _fit_sdv(method: str, data: pd.DataFrame) -> Any | None:
    """Best-effort SDV fit; returns None (→ fallback) when SDV is absent or errors."""
    try:
        from sdv.metadata import SingleTableMetadata  # noqa: PLC0415
    except Exception:
        return None
    try:  # pragma: no cover - SDV path, exercised only when the optional extra is installed
        from sdv.single_table import (  # noqa: PLC0415
            CTGANSynthesizer,
            GaussianCopulaSynthesizer,
            TVAESynthesizer,
        )

        metadata = SingleTableMetadata()
        metadata.detect_from_dataframe(data)
        cls = {
            "gaussian_copula": GaussianCopulaSynthesizer,
            "ctgan": CTGANSynthesizer,
            "tvae": TVAESynthesizer,
        }[method]
        model = cls(metadata)
        model.fit(data)
        return model
    except Exception as exc:  # pragma: no cover - SDV not installed in CI
        logger.warning("SDV fit failed (%s); falling back to pure-python copula", exc)
        return None


def fit_synthesizer(
    data: pd.DataFrame,
    method: str = "gaussian_copula",
    *,
    source_revision: str = "unknown",
    seed: int = 0,
) -> Synthesizer:
    """Fit a :class:`Synthesizer` to ``data`` (spec R1).

    Uses SDV when installed, else the pure-python fallback. ``method`` outside
    :data:`METHODS` raises ``ValueError`` (fail-loud on misconfiguration).
    """
    if method not in METHODS:
        raise ValueError(f"unknown synth method {method!r}; expected one of {METHODS}")
    if data.empty or len(data.columns) == 0:
        raise ValueError("cannot fit a synthesizer to an empty dataset")

    columns = list(data.columns)
    dtypes = {c: str(data[c].dtype) for c in columns}
    numeric_cols, categorical_cols, other_cols = [], [], []
    for c in columns:
        s = data[c]
        if _is_numeric(s):
            numeric_cols.append(c)
        elif _is_categorical(s) and _scalar_categorical(s):
            categorical_cols.append(c)
        else:
            other_cols.append(c)

    sdv_model = _fit_sdv(method, data)
    backend = "sdv" if sdv_model is not None else "fallback"
    synth = Synthesizer(
        method=method,
        source_revision=source_revision,
        backend=backend,
        columns=columns,
        dtypes=dtypes,
        numeric_cols=numeric_cols,
        categorical_cols=categorical_cols,
        other_cols=other_cols,
        seed=seed,
        _sdv_model=sdv_model,
    )
    if backend == "fallback":
        if method != "gaussian_copula":
            logger.info(
                "SDV unavailable — approximating method %r with the pure-python copula", method
            )
        _fit_fallback(synth, data)
    return synth


def _coerce_dtypes(df: pd.DataFrame, dtypes: dict[str, str]) -> pd.DataFrame:
    """Restore the original per-column dtypes so synthetic records match the schema (GWT-1)."""
    for col, dt in dtypes.items():
        if col not in df.columns:
            continue
        try:
            if dt.startswith(("int", "uint")):
                df[col] = np.rint(pd.to_numeric(df[col], errors="coerce")).astype(dt)
            else:
                df[col] = df[col].astype(dt)  # type: ignore[call-overload]  # dt is a runtime dtype string
        except Exception:  # pragma: no cover - defensive; keep the column rather than crash
            logger.debug("could not coerce column %s to %s", col, dt)
    return df


def generate(synth: Synthesizer, n: int, *, seed: int | None = None) -> pd.DataFrame:
    """Sample ``n`` synthetic records matching the fitted schema (spec R1, GWT-1)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if synth.backend == "sdv" and synth._sdv_model is not None:  # pragma: no cover - SDV path
        try:
            df = synth._sdv_model.sample(num_rows=n)
            df = df.reindex(columns=synth.columns)
            return _coerce_dtypes(df, synth.dtypes)
        except Exception as exc:
            logger.warning("SDV sample failed (%s); using fallback", exc)

    rng = np.random.default_rng(synth.seed if seed is None else seed)
    out: dict[str, Any] = {}

    # Numeric via Gaussian copula: correlated normals → uniforms → inverse empirical quantile.
    num = synth.numeric_cols
    if num:
        corr = synth._corr if synth._corr is not None else np.eye(len(num))
        try:
            z = rng.multivariate_normal(np.zeros(len(num)), corr, size=n, method="eigh")
        except Exception:  # pragma: no cover - non-PSD guard
            z = rng.standard_normal((n, len(num)))
        u = _normal_cdf(z)
        u = np.atleast_2d(u)
        for j, col in enumerate(num):
            q = synth._quantiles.get(col, np.zeros(1))
            out[col] = np.quantile(q, np.clip(u[:, j], 0.0, 1.0), method="linear")

    # Categorical via empirical frequency.
    for col in synth.categorical_cols:
        values, probs = synth._cat_dist.get(col, ([None], np.array([1.0])))
        idx = rng.choice(len(values), size=n, p=probs)
        out[col] = [values[i] for i in idx]

    # Other (lists/embeddings) via bootstrap resampling.
    for col in synth.other_cols:
        pool = synth._bootstrap.get(col) or [None]
        idx = rng.integers(0, len(pool), size=n)
        out[col] = [pool[i] for i in idx]

    df = pd.DataFrame({c: out.get(c) for c in synth.columns})
    return _coerce_dtypes(df, synth.dtypes)

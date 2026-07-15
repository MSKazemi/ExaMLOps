"""Built-in ``drift`` calculation providers (ADR 0077 — programmable MLOps, INC-5).

Extends the ``examlops.providers`` substrate to the drift domain: the z-score classifier that
determines whether a model's prediction distribution has drifted from its baseline is now a
swappable **provider**, so operators can change the scoring methodology (e.g. KL-divergence,
PSI, a learned detector) via config or a plugin — without touching core code.

Built-in providers:

* ``z-score`` (**default**) — the platform's original z-score classifier, byte-for-byte.
  ``z = |live_mean - baseline_mean| / baseline_std``; thresholds ``warn_z=2.0``, ``crit_z=3.0``.

Importing this module registers the built-ins as a side effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .providers import Provider, ProviderMeta, register_provider
from .providers.loader import resolve_provider

DOMAIN = "drift"

DEFAULT_WARN_Z = 2.0
DEFAULT_CRIT_Z = 3.0


def _f(inputs: Mapping[str, Any], key: str, default: float) -> float:
    val = inputs.get(key, default)
    return float(val if val is not None else default)


class ZScoreDriftProvider(Provider):
    """Default drift classifier — z-score distance from the baseline distribution.

    ``compute`` returns ``{"z_score": …, "status": …}`` byte-identical to the platform's
    original inline logic in ``cli/commands/drift.py:_drift_rows``.
    """

    name = "z-score"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "z = |live_mean − baseline_mean| / baseline_std; "
                "CRITICAL if z ≥ crit_z (3.0), WARNING if z ≥ warn_z (2.0), else OK."
            ),
            outputs=("z_score", "status"),
            params=("live_mean", "baseline_mean", "baseline_std", "warn_z", "crit_z"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        baseline_std = _f(inputs, "baseline_std", 0.0)
        if baseline_std == 0.0:
            return {"z_score": 0.0, "status": "OK (no baseline)"}
        live_mean = _f(inputs, "live_mean", 0.0)
        baseline_mean = _f(inputs, "baseline_mean", 0.0)
        warn_z = _f(inputs, "warn_z", DEFAULT_WARN_Z)
        crit_z = _f(inputs, "crit_z", DEFAULT_CRIT_Z)
        z = abs(live_mean - baseline_mean) / baseline_std
        if z >= crit_z:
            status = "CRITICAL"
        elif z >= warn_z:
            status = "WARNING"
        else:
            status = "OK"
        return {"z_score": z, "status": status}


def register_builtins() -> None:
    """Register the built-in drift providers on the global registry (idempotent)."""
    register_provider(DOMAIN, "z-score", ZScoreDriftProvider, default=True)


def resolve_drift_score_fn(override: str | None = None):
    """Resolve the active drift provider and return a callable scorer.

    Returns a function ``(live_mean, live_std, baseline) -> (z_score, status)`` where
    ``baseline`` is the dict from ``get_drift_baseline`` (may be None).

    Precedence (via :func:`resolve_provider`): ``override`` → ``EXAMLOPS_DRIFT_PROVIDER`` env →
    ``providers.yaml`` ``drift:`` block → built-in ``z-score`` default.

    A resolution/compute failure degrades to the built-in z-score logic so drift detection
    never breaks (graceful-degradation invariant).
    """
    register_builtins()
    try:
        provider = resolve_provider(DOMAIN, override=override, group=DOMAIN)
    except Exception:
        provider = None

    def _score(live_mean: float, live_std: float, baseline: dict | None) -> tuple[float, str]:
        if baseline is None or baseline.get("std", 0.0) == 0.0:
            return 0.0, "OK (no baseline)"
        inputs = {
            "live_mean": live_mean,
            "live_std": live_std,
            "baseline_mean": baseline["mean"],
            "baseline_std": baseline["std"],
            "warn_z": DEFAULT_WARN_Z,
            "crit_z": DEFAULT_CRIT_Z,
        }
        try:
            if provider is not None:
                out = provider.compute(inputs)
                return float(out["z_score"]), str(out["status"])
        except Exception:
            pass
        # Graceful degradation: inline z-score
        z = abs(live_mean - baseline["mean"]) / baseline["std"]
        if z >= DEFAULT_CRIT_Z:
            status = "CRITICAL"
        elif z >= DEFAULT_WARN_Z:
            status = "WARNING"
        else:
            status = "OK"
        return z, status

    return _score


# Register at import time (matches carbon/cost/placement convention).
register_builtins()

"""Built-in ``promotion`` calculation providers (ADR 0077 — programmable MLOps, INC-5).

Extends the ``examlops.providers`` substrate to the promotion domain: the threshold evaluation
that decides whether a model version should be promoted from Staging → Production is now a
swappable **provider**, so operators can plug in custom scoring (e.g. multi-metric gates, Pareto
checks, ensemble confidence thresholds) without touching core code.

Built-in providers:

* ``threshold`` (**default**) — the platform's original single-metric threshold comparison,
  byte-for-byte. Supports operators lt/lte/gt/gte.

Importing this module registers the built-ins as a side effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .providers import Provider, ProviderMeta, register_provider
from .providers.loader import resolve_provider

DOMAIN = "promotion"

_OPS = {
    "lt": lambda v, t: v < t,
    "gt": lambda v, t: v > t,
    "lte": lambda v, t: v <= t,
    "gte": lambda v, t: v >= t,
}


def _f(inputs: Mapping[str, Any], key: str, default: float) -> float:
    val = inputs.get(key, default)
    return float(val if val is not None else default)


class ThresholdPromotionProvider(Provider):
    """Default promotion evaluator — single-metric threshold gate.

    ``compute`` returns ``{"passes": bool, "reason": str}`` byte-identical to the platform's
    original inline logic in ``cli/commands/pipeline.py:promote``.
    """

    name = "threshold"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "passes = op(metric_val, threshold) where op ∈ {lt, lte, gt, gte}. "
                "Single-metric gate; the default for all ``exa pipeline promote`` calls."
            ),
            outputs=("passes", "reason"),
            params=("metric_val", "threshold", "operator"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        metric_val = _f(inputs, "metric_val", 0.0)
        threshold = _f(inputs, "threshold", 0.0)
        operator = str(inputs.get("operator", "lt"))
        op_fn = _OPS.get(operator)
        if op_fn is None:
            return {"passes": False, "reason": f"unknown operator '{operator}'"}
        passes = bool(op_fn(metric_val, threshold))
        op_sym = "<" if operator in ("lt", "lte") else ">"
        reason = (
            f"{metric_val:.4f} {op_sym} {threshold}"
            if passes
            else f"{metric_val:.4f} not {op_sym} {threshold}"
        )
        return {"passes": passes, "reason": reason}


def register_builtins() -> None:
    """Register the built-in promotion providers on the global registry (idempotent)."""
    register_provider(DOMAIN, "threshold", ThresholdPromotionProvider, default=True)


def resolve_promotion_eval_fn(override: str | None = None):
    """Resolve the active promotion provider and return a callable evaluator.

    Returns a function ``(metric_val, threshold, operator) -> (passes, reason)`` where
    ``passes`` is True if the metric passes the threshold gate.

    Precedence (via :func:`resolve_provider`): ``override`` → ``EXAMLOPS_PROMOTION_PROVIDER``
    env → ``providers.yaml`` ``promotion:`` block → built-in ``threshold`` default.

    A resolution/compute failure degrades to the built-in threshold logic so promotion
    never silently breaks (graceful-degradation invariant).
    """
    register_builtins()
    try:
        provider = resolve_provider(DOMAIN, override=override, group=DOMAIN)
    except Exception:
        provider = None

    def _eval(metric_val: float, threshold: float, operator: str) -> tuple[bool, str]:
        inputs = {
            "metric_val": metric_val,
            "threshold": threshold,
            "operator": operator,
        }
        try:
            if provider is not None:
                out = provider.compute(inputs)
                return bool(out["passes"]), str(out["reason"])
        except Exception:
            pass
        # Graceful degradation: inline threshold logic
        op_fn = _OPS.get(operator, lambda v, t: False)
        passes = bool(op_fn(metric_val, threshold))
        op_sym = "<" if operator in ("lt", "lte") else ">"
        reason = (
            f"{metric_val:.4f} {op_sym} {threshold}"
            if passes
            else f"{metric_val:.4f} not {op_sym} {threshold}"
        )
        return passes, reason

    return _eval


# Register at import time (matches carbon/cost/placement convention).
register_builtins()

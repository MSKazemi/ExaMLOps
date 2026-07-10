"""Built-in ``cost`` calculation providers — the substrate's second domain (ADR 0074).

Proves ``examlops.providers`` is reusable beyond carbon: a **cost provider** turns scheduler usage
into a USD figure via a swappable rate card. Importing this module registers the built-ins.

* ``flat-rate`` (**default**) — ``gpu_hours × gpu_rate + cpu_hours × cpu_rate``, byte-for-byte the
  platform's original cost arithmetic (same env-overridable defaults).
* ``tiered-example`` — a demonstration of a non-trivial rate card (a volume discount above a
  GPU-hours threshold), showing that the formula — not just the coefficients — is swappable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..providers import Provider, ProviderMeta, register_provider
from . import cost


def _f(inputs: Mapping[str, Any], key: str, default: float) -> float:
    val = inputs.get(key, default)
    return float(val if val is not None else default)


class FlatRateProvider(Provider):
    """The original rate card — delegates to the untouched pure function."""

    name = "flat-rate"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology="cost_usd = gpu_hours × gpu_rate + cpu_hours × cpu_rate (flat rate card).",
            units={"cost_usd": "USD"},
            outputs=("cost_usd",),
            params=("gpu_hours", "cpu_hours", "gpu_rate", "cpu_rate"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        gpu_hours = _f(inputs, "gpu_hours", 0.0)
        cpu_hours = _f(inputs, "cpu_hours", 0.0)
        gpu_rate = inputs.get("gpu_rate")
        cpu_rate = inputs.get("cpu_rate")
        return {
            "cost_usd": cost.flat_rate_cost(
                gpu_hours, cpu_hours, gpu_rate=gpu_rate, cpu_rate=cpu_rate
            )
        }


class TieredProvider(Provider):
    """Example volume-discounted rate card: cheaper GPU-hours above a threshold."""

    name = "tiered-example"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "GPU-hours up to `tier_threshold` at `gpu_rate`, the remainder at "
                "`gpu_rate × (1 - tier_discount)`; plus cpu_hours × cpu_rate."
            ),
            units={"cost_usd": "USD"},
            outputs=("cost_usd",),
            params=(
                "gpu_hours",
                "cpu_hours",
                "gpu_rate",
                "cpu_rate",
                "tier_threshold",
                "tier_discount",
            ),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        gpu_hours = _f(inputs, "gpu_hours", 0.0)
        cpu_hours = _f(inputs, "cpu_hours", 0.0)
        gpu_rate = _f(inputs, "gpu_rate", cost.default_gpu_rate())
        cpu_rate = _f(inputs, "cpu_rate", cost.default_cpu_rate())
        threshold = _f(inputs, "tier_threshold", 100.0)
        discount = _f(inputs, "tier_discount", 0.2)
        if gpu_hours < 0 or cpu_hours < 0:
            raise ValueError("hours must be non-negative")
        base = min(gpu_hours, threshold) * gpu_rate
        over = max(0.0, gpu_hours - threshold) * gpu_rate * (1.0 - discount)
        return {"cost_usd": round(base + over + cpu_hours * cpu_rate, 4)}


def register_builtins() -> None:
    """Register the built-in cost providers on the global registry (idempotent)."""
    register_provider("cost", "flat-rate", FlatRateProvider, default=True)
    register_provider("cost", "tiered-example", TieredProvider)


register_builtins()

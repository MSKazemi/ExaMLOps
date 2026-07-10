"""Pluggable placement-scoring providers (ADR 0077 — programmable MLOps, surface S2).

Placement is the reference domain that proves the `examlops.providers` substrate generalizes beyond
finops: the single scoring function `hpc_placement.headroom_score` becomes a swappable **provider**,
so an operator can change *how the fleet places jobs* — carbon-aware, fair-share, cost-aware — with

* a declarative ``providers.yaml`` formula (no code), or
* a ``pip``-installed plugin under the ``exa.providers.placement`` entry-point group, or
* the built-in default (``least-loaded``), which reproduces ``headroom_score`` **byte-for-byte**.

This module keeps `hpc_placement.py` pure: it resolves the active provider here and adapts it into the
plain ``(ask, cap) -> float`` callable `choose_cluster` expects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .hpc_placement import ResourceAsk, ScoreFn, headroom_score
from .providers import Provider, ProviderMeta, register_provider
from .providers.loader import resolve_provider

DOMAIN = "placement"


def _score_inputs(ask: ResourceAsk, cap: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten (ask, capacity) into the namespace a provider/formula scores over.

    Capacity keys (idle_gpus/total_gpus/idle_nodes/total_nodes/idle_cpus) pass through as-is, so a
    formula can reference them directly; ask fields are prefixed ``ask_``. Any extra capacity keys
    (e.g. a future ``carbon_g``) also pass through, enabling carbon-aware formulas with no code change.
    """
    inputs: dict[str, Any] = dict(cap)
    inputs.setdefault("idle_gpus", 0)
    inputs.setdefault("idle_nodes", 0)
    inputs["ask_gpus"] = ask.gpus
    inputs["ask_cpus"] = ask.cpus
    inputs["ask_nodes"] = ask.nodes
    return inputs


class LeastLoadedProvider(Provider):
    """Default placement policy — most idle headroom after the ask (GPUs weighted heaviest).

    ``compute`` returns ``{"score": …}`` equal to ``headroom_score`` for the same inputs, so routing
    through the provider substrate is a no-op versus the legacy path (backward-compat invariant).
    """

    name = "least-loaded"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology="(idle_gpus - ask_gpus) * 100 + (idle_nodes - ask_nodes) — least-loaded",
            outputs=("score",),
            params=("idle_gpus", "idle_nodes", "ask_gpus", "ask_nodes"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        idle_gpus = inputs.get("idle_gpus", 0)
        idle_nodes = inputs.get("idle_nodes", 0)
        ask_gpus = inputs.get("ask_gpus", 0)
        ask_nodes = inputs.get("ask_nodes", 0)
        return {"score": (idle_gpus - ask_gpus) * 100 + (idle_nodes - ask_nodes)}


def register_builtins() -> None:
    """Register the built-in placement providers on the global registry (idempotent)."""
    register_provider(DOMAIN, "least-loaded", LeastLoadedProvider, default=True)


def resolve_placement_score_fn(override: str | None = None) -> ScoreFn:
    """Resolve the active placement provider and adapt it to a ``(ask, cap) -> float`` scorer.

    Precedence (via :func:`resolve_provider`): ``override`` (CLI ``--placement-provider``) →
    ``EXAMLOPS_PLACEMENT_PROVIDER`` env → ``providers.yaml`` ``placement:`` block → built-in
    ``least-loaded`` default. A resolution/compute failure degrades to :func:`headroom_score` so
    placement never breaks (graceful-degradation invariant).
    """
    register_builtins()
    try:
        provider = resolve_provider(DOMAIN, override=override, group=DOMAIN)
    except Exception:
        return headroom_score

    def _score(ask: ResourceAsk, cap: dict) -> float:
        try:
            out = provider.compute(_score_inputs(ask, cap))
            return float(out["score"])
        except Exception:
            return headroom_score(ask, cap)

    return _score


# Registering at import time matches the finops convention ("importing registers the built-ins"),
# so `exa providers list` and any consumer see the placement default without an explicit call.
register_builtins()

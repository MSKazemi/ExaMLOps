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
from .providers.loader import degraded_to_default, resolve_provider

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


class _GreenProvider(Provider):
    """Base for carbon-/cost-aware placement (Phase 5 item 5.3).

    Starts from the least-loaded headroom score, then penalizes a cluster's **grid carbon intensity**
    (``carbon_intensity`` gCO₂e/kWh) and **GPU cost** (``cost_per_gpu_hour`` USD) — both passed
    through from the cluster's declared capabilities (or a live signal). A cluster with no such data
    scores exactly like least-loaded, so this degrades to the default where the signal is absent.
    Subclasses set the weights, so operators get carbon-first, cost-first, or balanced placement by
    selecting a provider name — no code change (ADR 0077).
    """

    _W_CARBON = 0.0
    _W_COST = 0.0

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                f"headroom − {self._W_CARBON}·carbon_intensity − {self._W_COST}·cost_per_gpu_hour·100"
            ),
            outputs=("score",),
            params=("idle_gpus", "idle_nodes", "carbon_intensity", "cost_per_gpu_hour"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        base = (inputs.get("idle_gpus", 0) - inputs.get("ask_gpus", 0)) * 100 + (
            inputs.get("idle_nodes", 0) - inputs.get("ask_nodes", 0)
        )
        score = float(base)
        ci = inputs.get("carbon_intensity")
        if ci is not None:
            score -= self._W_CARBON * float(ci)
        cost = inputs.get("cost_per_gpu_hour")
        if cost is not None:
            score -= self._W_COST * float(cost) * 100.0
        return {"score": score}


class CarbonAwareProvider(_GreenProvider):
    """Prefer the greenest cluster that fits (carbon penalty dominates)."""

    name = "carbon-aware"
    version = "1.0"
    _W_CARBON = 2.0
    _W_COST = 0.5
    #: Carbon is this policy's purpose, so when the R-ec gate refuses it the simple carbon
    #: baseline runs in its place; for a policy where carbon is one term among several the gate
    #: instead withholds the carbon input and leaves the other objectives working.
    carbon_primary = True


class LowestIntensityProvider(Provider):
    """The simple carbon baseline ADR 0112 R-ec names: lowest declared intensity first.

    Among clusters that fit, the one with the lowest ``carbon_intensity`` wins outright; headroom
    only breaks ties. A cluster with no intensity data ranks after every cluster that has some —
    unknown is not green (the flaw of scoring a missing value as a zero penalty). This is what
    ships whenever a more sophisticated carbon policy has not proven it does better.
    """

    name = "carbon-simple"
    version = "1.0"
    _SCALE = 1e9  # one gCO2e/kWh outweighs any realistic headroom difference
    _UNKNOWN = 1e4  # an intensity above every real grid (~0–1,500 g/kWh)

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology="lowest carbon_intensity first; headroom breaks ties; unknown ranks last",
            outputs=("score",),
            params=("carbon_intensity", "idle_gpus", "idle_nodes"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        headroom = (inputs.get("idle_gpus", 0) - inputs.get("ask_gpus", 0)) * 100 + (
            inputs.get("idle_nodes", 0) - inputs.get("ask_nodes", 0)
        )
        ci = inputs.get("carbon_intensity")
        intensity = self._UNKNOWN if ci is None else float(ci)
        return {"score": float(headroom) - self._SCALE * intensity}


class CostAwareProvider(_GreenProvider):
    """Prefer the cheapest cluster that fits (cost penalty dominates)."""

    name = "cost-aware"
    version = "1.0"
    _W_CARBON = 0.5
    _W_COST = 2.0


class BalancedGreenProvider(_GreenProvider):
    """Balance headroom, carbon, and cost."""

    name = "carbon-cost-balanced"
    version = "1.0"
    _W_CARBON = 1.0
    _W_COST = 1.0


def register_builtins() -> None:
    """Register the built-in placement providers on the global registry (idempotent)."""
    register_provider(DOMAIN, "least-loaded", LeastLoadedProvider, default=True)
    register_provider(DOMAIN, "carbon-aware", CarbonAwareProvider)
    register_provider(DOMAIN, "cost-aware", CostAwareProvider)
    register_provider(DOMAIN, "carbon-cost-balanced", BalancedGreenProvider)
    register_provider(DOMAIN, "carbon-simple", LowestIntensityProvider)


_CARBON_KEYS = ("carbon_intensity", "carbon_intensity_decision")
#: Withholding carbon *neutralises* it rather than deleting it: every cluster is scored as if it
#: had this same intensity, so carbon cannot change the ranking while the policy's other terms
#: (cost, headroom) still do. Deleting the key would make a YAML formula that names
#: ``carbon_intensity`` fail and fall back to bare headroom, dropping its other objectives too.
_NEUTRAL_INTENSITY = 1.0


def weighs_carbon(provider: Provider) -> bool:
    """Does this policy's score depend on carbon intensity? Decided by probing, not by name.

    Two otherwise identical clusters that differ only in ``carbon_intensity`` are scored; if the
    scores differ, the policy weighs carbon and falls under ADR 0112 R-ec. Probing covers every
    provider kind alike — built-ins, YAML formulas, pip-installed plugins — where reading
    metadata would trust whatever a plugin chose to declare. A provider whose probe raises is
    treated as carbon-weighing: unknown is gated, never waved through.
    """
    base = {
        "idle_gpus": 4,
        "total_gpus": 8,
        "idle_nodes": 2,
        "total_nodes": 4,
        "ask_gpus": 1,
        "ask_cpus": 0,
        "ask_nodes": 1,
        "cost_per_gpu_hour": 1.0,
    }
    try:
        low = float(provider.compute({**base, "carbon_intensity": 10.0})["score"])
        high = float(provider.compute({**base, "carbon_intensity": 900.0})["score"])
    except Exception:  # noqa: BLE001
        return True
    return low != high


def _adapt(provider: Provider, *, strip_carbon: bool) -> ScoreFn:
    def _score(ask: ResourceAsk, cap: dict) -> float:
        view = {**cap, **dict.fromkeys(_CARBON_KEYS, _NEUTRAL_INTENSITY)} if strip_carbon else cap
        try:
            out = provider.compute(_score_inputs(ask, view))
            return float(out["score"])
        except Exception as exc:  # noqa: BLE001 - a broken provider must not stop the calculation
            degraded_to_default(DOMAIN, exc)
            return headroom_score(ask, view)

    return _score


def resolve_placement_score_fn(override: str | None = None) -> ScoreFn:
    """Resolve the active placement provider and adapt it to a ``(ask, cap) -> float`` scorer.

    Precedence (via :func:`resolve_provider`): ``override`` (CLI ``--placement-provider``) →
    ``EXAMLOPS_PLACEMENT_PROVIDER`` env → ``providers.yaml`` ``placement:`` block → built-in
    ``least-loaded`` default. A resolution/compute failure degrades to :func:`headroom_score` so
    placement never breaks (graceful-degradation invariant).

    **The R-ec / R-ed gate (ADR 0112).** A policy that weighs carbon may do so only while it holds
    a passing, current evaluation against the simple baselines
    (:mod:`examlops.finops.carbon_policy`). Otherwise a carbon-first policy is replaced by the
    simple baseline (``carbon-simple``), any other carbon-weighing policy runs with its carbon
    input withheld (neutralised: every cluster scored at one intensity), and a retired capability
    gets no carbon input at all. What happened is
    attached to the returned scorer as ``placement_policy`` and reported by ``choose_cluster``.
    """
    register_builtins()
    try:
        provider = resolve_provider(DOMAIN, override=override, group=DOMAIN)
    except Exception as exc:  # noqa: BLE001 - a broken provider must not stop the calculation
        degraded_to_default(DOMAIN, exc)
        return headroom_score

    requested = getattr(provider, "name", str(override or "unnamed"))
    if not weighs_carbon(provider):
        return _adapt(provider, strip_carbon=False)

    from examlops.finops.carbon_policy import RUNTIME_SIMPLE, placement_gate

    notes: list[str] = []
    try:
        decision = placement_gate(
            requested, carbon_primary=bool(getattr(provider, "carbon_primary", False))
        )
    except Exception as exc:  # noqa: BLE001 - a broken gate fails closed, never open
        from examlops.finops.carbon_policy import GateDecision

        decision = GateDecision(requested, "withhold", f"carbon-policy gate failed: {exc}")
    effective, action = requested, decision.action
    notes.extend(decision.notes)
    if action == "substitute":
        provider = resolve_provider(DOMAIN, override=RUNTIME_SIMPLE, group=DOMAIN)
        effective = RUNTIME_SIMPLE
        simple = placement_gate(RUNTIME_SIMPLE, carbon_primary=True)
        notes.extend(simple.notes)
        if simple.action != "allow":  # the simple baseline itself is retired
            action, notes = "agnostic", [*notes, simple.reason]
    score = _adapt(provider, strip_carbon=action in ("withhold", "agnostic"))
    score.placement_policy = {  # type: ignore[attr-defined]
        "requested": requested,
        "effective": effective,
        "action": action,
        "carbon_input": action in ("allow", "substitute"),
        "reason": decision.reason,
        "evaluation_event": decision.evaluation_event,
        "notes": notes,
    }
    return score


# Registering at import time matches the finops convention ("importing registers the built-ins"),
# so `exa providers list` and any consumer see the placement default without an explicit call.
register_builtins()

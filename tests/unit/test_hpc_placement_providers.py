"""Unit tests for pluggable placement providers (INC-1 / ADR 0077).

Proves the vision-card thesis: the finops provider pattern generalizes to a non-finops domain
(placement) — a config formula or a plugin changes placement with zero core edits — while the default
stays byte-identical to the legacy ``headroom_score``.
"""

from __future__ import annotations

import pytest

from examlops.hpc_placement import ResourceAsk, choose_cluster, headroom_score
from examlops.hpc_placement_providers import (
    LeastLoadedProvider,
    resolve_placement_score_fn,
)
from examlops.providers import get_provider


def _cluster(name, scheduler, nodes=None, caps=None):
    return {"name": name, "scheduler": scheduler, "nodes": nodes or [], "capabilities": caps}


def _node(state="idle", gpus=0, cpus=32):
    return {"state": state, "gpus": gpus, "cpus": cpus}


# ── the default provider is byte-identical to the legacy scorer ────────────────────────────────
@pytest.mark.parametrize(
    "idle_gpus,idle_nodes,ask_gpus,ask_nodes",
    [(4, 2, 2, 1), (8, 4, 0, 1), (1, 1, 1, 1), (16, 8, 4, 2)],
)
def test_least_loaded_provider_matches_headroom_score(idle_gpus, idle_nodes, ask_gpus, ask_nodes):
    ask = ResourceAsk(gpus=ask_gpus, nodes=ask_nodes)
    cap = {"idle_gpus": idle_gpus, "idle_nodes": idle_nodes}
    provider = LeastLoadedProvider()
    got = provider.compute(
        {
            "idle_gpus": idle_gpus,
            "idle_nodes": idle_nodes,
            "ask_gpus": ask_gpus,
            "ask_nodes": ask_nodes,
        }
    )["score"]
    assert got == headroom_score(ask, cap)


def test_default_resolution_reproduces_default_choice():
    """resolve_placement_score_fn() with no config == the legacy default placement."""
    busy = _cluster("busy", "flux", nodes=[_node("idle", 2), _node("allocated", 4)])
    free = _cluster("free", "slurm", nodes=[_node("idle", 4), _node("idle", 4)])
    default = choose_cluster(ResourceAsk(gpus=2), [busy, free])
    via_provider = choose_cluster(ResourceAsk(gpus=2), [busy, free], resolve_placement_score_fn())
    assert via_provider.cluster == default.cluster == "free"
    assert via_provider.candidates[0]["score"] == default.candidates[0]["score"]


# ── a config formula changes placement with ZERO core edits (the thesis) ───────────────────────
def test_expression_formula_reranks_placement():
    """A declarative formula weighting nodes over GPUs picks a different cluster than the default."""
    # 'gpu_rich' wins by default (more idle GPUs); 'node_rich' wins under a node-weighted formula.
    gpu_rich = _cluster("gpu_rich", "flux", nodes=[_node("idle", 8)])
    node_rich = _cluster(
        "node_rich", "slurm", nodes=[_node("idle", 1), _node("idle", 1), _node("idle", 1)]
    )
    default = choose_cluster(ResourceAsk(gpus=1), [gpu_rich, node_rich])
    assert default.cluster == "gpu_rich"

    provider = get_provider(
        "placement", "expression", config={"formulas": {"score": "idle_nodes * 1000 + idle_gpus"}}
    )

    def score_fn(ask, cap):
        return float(
            provider.compute({**cap, "ask_gpus": ask.gpus, "ask_nodes": ask.nodes})["score"]
        )

    reranked = choose_cluster(ResourceAsk(gpus=1), [gpu_rich, node_rich], score_fn)
    assert reranked.cluster == "node_rich"  # changed placement, no core edit


def test_carbon_aware_formula_prefers_greener_cluster():
    """A carbon-aware formula placing on the greener cluster despite fewer idle GPUs."""
    dirty = _cluster(
        "dirty", "slurm", caps={"total_gpus": 8, "total_nodes": 2, "carbon_intensity": 500}
    )
    green = _cluster(
        "green", "flux", caps={"total_gpus": 4, "total_nodes": 1, "carbon_intensity": 50}
    )
    # Default (headroom): 'dirty' wins (more idle GPUs).
    assert choose_cluster(ResourceAsk(gpus=1), [dirty, green]).cluster == "dirty"

    provider = get_provider(
        "placement",
        "expression",
        config={"formulas": {"score": "idle_gpus * 100 - carbon_intensity"}},
    )

    def score_fn(ask, cap):
        return float(
            provider.compute({**cap, "ask_gpus": ask.gpus, "ask_nodes": ask.nodes})["score"]
        )

    greener = choose_cluster(ResourceAsk(gpus=1), [dirty, green], score_fn)
    assert greener.cluster == "green"  # carbon-aware placement, zero core edits


# ── graceful degradation: a broken provider falls back to the default ──────────────────────────
def test_broken_provider_degrades_to_headroom(monkeypatch):
    import examlops.hpc_placement_providers as mod

    def boom(*a, **k):
        raise RuntimeError("plugin exploded")

    monkeypatch.setattr(mod, "resolve_provider", boom)
    score_fn = mod.resolve_placement_score_fn()
    # Falls back to headroom_score → default placement still works.
    free = _cluster("free", "slurm", nodes=[_node("idle", 4)])
    busy = _cluster("busy", "flux", nodes=[_node("idle", 1)])
    result = choose_cluster(ResourceAsk(gpus=1), [busy, free], score_fn)
    assert result.cluster == "free"


def test_provider_compute_error_degrades_per_candidate(monkeypatch):
    """If a resolved provider raises at compute time, scoring falls back to headroom per candidate."""
    import examlops.hpc_placement_providers as mod

    class Exploding:
        def compute(self, inputs):
            raise ValueError("bad formula")

    monkeypatch.setattr(mod, "resolve_provider", lambda *a, **k: Exploding())
    score_fn = mod.resolve_placement_score_fn()
    free = _cluster("free", "slurm", nodes=[_node("idle", 4)])
    busy = _cluster("busy", "flux", nodes=[_node("idle", 1)])
    result = choose_cluster(ResourceAsk(gpus=1), [busy, free], score_fn)
    assert result.cluster == "free"  # degraded to headroom, still sane


def test_registration_is_idempotent_and_default():
    from examlops.hpc_placement_providers import register_builtins
    from examlops.providers import default_provider_name, list_providers

    register_builtins()
    register_builtins()  # idempotent
    names = {i.name for i in list_providers("placement")}
    assert "least-loaded" in names
    assert default_provider_name("placement") == "least-loaded"

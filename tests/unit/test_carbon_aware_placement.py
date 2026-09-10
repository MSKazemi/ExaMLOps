"""Carbon/cost-aware placement (enterprise-readiness Phase 5, item 5.3).

Proves the placement scoring fn can factor grid carbon intensity + GPU cost (from cluster
capabilities) so the fleet steers jobs to the greenest/cheapest cluster that fits — while
degrading to least-loaded when no carbon/cost signal is present (backward compatible).
"""

from __future__ import annotations

import pytest

from examlops.hpc_placement import ResourceAsk, choose_cluster, headroom_score
from examlops.hpc_placement_providers import resolve_placement_score_fn

_ASK = ResourceAsk(gpus=1, nodes=1)


@pytest.fixture(autouse=True)
def _formulas_not_governance(monkeypatch, tmp_path):
    """These tests pin each provider's *scoring formula*. Whether a carbon-weighing policy may
    run at all is ADR 0112's R-ec gate, tested in test_carbon_policy.py; in `warn` mode the gate
    records its verdict and lets the requested policy score, which is what a formula test needs.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CARBON_POLICY_GATE", "warn")


def _cluster(name, *, idle_gpus, carbon=None, cost=None):
    caps = {"total_gpus": idle_gpus, "total_nodes": 2}
    if carbon is not None:
        caps["carbon_intensity"] = carbon
    if cost is not None:
        caps["cost_per_gpu_hour"] = cost
    # No live nodes → _effective_capacity uses declared capabilities (idle == total).
    return {"name": name, "scheduler": "flux", "capabilities": caps, "nodes": []}


def test_least_loaded_default_unchanged():
    fn = resolve_placement_score_fn("least-loaded")
    cap = {"idle_gpus": 4, "idle_nodes": 2}
    assert fn(_ASK, cap) == headroom_score(_ASK, cap)  # byte-for-byte default


def test_carbon_aware_prefers_greener_cluster():
    clusters = [
        _cluster("dirty", idle_gpus=8, carbon=600),  # more headroom but dirty grid
        _cluster("green", idle_gpus=4, carbon=40),  # less headroom, clean grid
    ]
    result = choose_cluster(_ASK, clusters, resolve_placement_score_fn("carbon-aware"))
    assert result.cluster == "green"  # carbon penalty overrides the headroom edge


def test_cost_aware_prefers_cheaper_cluster():
    clusters = [
        _cluster("pricey", idle_gpus=8, cost=5.0),
        _cluster("cheap", idle_gpus=4, cost=0.5),
    ]
    result = choose_cluster(_ASK, clusters, resolve_placement_score_fn("cost-aware"))
    assert result.cluster == "cheap"


def test_no_signal_falls_back_to_headroom():
    """With no carbon/cost data, carbon-aware picks the same cluster as least-loaded."""
    clusters = [_cluster("a", idle_gpus=8), _cluster("b", idle_gpus=2)]
    green = choose_cluster(_ASK, clusters, resolve_placement_score_fn("carbon-aware"))
    base = choose_cluster(_ASK, clusters, resolve_placement_score_fn("least-loaded"))
    assert green.cluster == base.cluster == "a"  # most headroom


def test_balanced_weighs_all_three():
    clusters = [
        _cluster("big-dirty", idle_gpus=10, carbon=500, cost=3.0),
        _cluster("small-green-cheap", idle_gpus=5, carbon=30, cost=0.4),
    ]
    result = choose_cluster(_ASK, clusters, resolve_placement_score_fn("carbon-cost-balanced"))
    assert result.cluster == "small-green-cheap"

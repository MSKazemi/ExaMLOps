"""Unit tests for scheduler-aware placement (Phase 35c, pure/offline)."""

from __future__ import annotations

from examlops.hpc_placement import (
    ResourceAsk,
    can_satisfy,
    choose_cluster,
    node_capacity,
)


def _cluster(name, scheduler, nodes=None, caps=None):
    return {"name": name, "scheduler": scheduler, "nodes": nodes or [], "capabilities": caps}


def _node(state="idle", gpus=0, cpus=32):
    return {"state": state, "gpus": gpus, "cpus": cpus}


def test_node_capacity_counts_idle_vs_total():
    nodes = [_node("idle", 4), _node("allocated", 4), _node("idle", 0)]
    cap = node_capacity(nodes)
    assert cap["total_nodes"] == 3
    assert cap["idle_nodes"] == 2
    assert cap["total_gpus"] == 8
    assert cap["idle_gpus"] == 4


def test_can_satisfy_respects_totals():
    cap = node_capacity([_node("allocated", 4)])
    assert can_satisfy(ResourceAsk(gpus=4, nodes=1), cap) is True
    assert can_satisfy(ResourceAsk(gpus=8, nodes=1), cap) is False


def test_choose_picks_cluster_with_more_idle_gpus():
    busy = _cluster("busy", "flux", nodes=[_node("idle", 2), _node("allocated", 4)])
    free = _cluster("free", "slurm", nodes=[_node("idle", 4), _node("idle", 4)])
    result = choose_cluster(ResourceAsk(gpus=2), [busy, free])
    assert result.cluster == "free"
    assert "idle GPUs" in result.reason


def test_choose_returns_none_when_ask_exceeds_all_capacity():
    small = _cluster("small", "flux", nodes=[_node("idle", 1)])
    result = choose_cluster(ResourceAsk(gpus=8), [small])
    assert result.cluster is None
    assert "no ACTIVE cluster can satisfy" in result.reason
    assert result.candidates[0]["fits"] is False


def test_choose_with_no_clusters():
    result = choose_cluster(ResourceAsk(gpus=1), [])
    assert result.cluster is None
    assert "no ACTIVE clusters registered" in result.reason


def test_choose_falls_back_to_declared_capabilities_without_snapshot():
    # No node snapshot, but capabilities declare 8 GPUs → still a valid candidate.
    c = _cluster("remote", "flux", nodes=[], caps={"total_gpus": 8, "total_nodes": 2})
    result = choose_cluster(ResourceAsk(gpus=4, nodes=1), [c])
    assert result.cluster == "remote"


def test_cpu_only_ask_places_on_gpuless_cluster():
    cpu = _cluster("cpu", "slurm", nodes=[_node("idle", 0, cpus=64)])
    result = choose_cluster(ResourceAsk(gpus=0, nodes=1), [cpu])
    assert result.cluster == "cpu"

"""Prometheus SD generation from the fleet registry (enterprise-readiness Phase 3, item 3.1).

Proves scrape targets are generated from registry nodes: node_exporter for every node, DCGM only for
GPU nodes, grouped by cluster with cluster/scheduler/tenant labels, and written as valid file_sd JSON.
"""

from __future__ import annotations

import json

import pytest

from examlops.prometheus_sd import (
    DCGM_EXPORTER_PORT,
    NODE_EXPORTER_PORT,
    node_targets,
)


def _node(cluster, name, gpus, scheduler="flux"):
    return {"cluster": cluster, "node": name, "gpus": gpus, "scheduler": scheduler}


def test_node_exporter_target_for_every_node():
    groups = node_targets([_node("remote", "n0", 0), _node("remote", "n1", 8)])
    node_group = next(g for g in groups if g["labels"]["job"] == "node")
    assert f"n0:{NODE_EXPORTER_PORT}" in node_group["targets"]
    assert f"n1:{NODE_EXPORTER_PORT}" in node_group["targets"]


def test_dcgm_only_for_gpu_nodes():
    groups = node_targets([_node("remote", "cpu0", 0), _node("remote", "gpu0", 4)])
    dcgm = next(g for g in groups if g["labels"]["job"] == "dcgm")
    assert dcgm["targets"] == [f"gpu0:{DCGM_EXPORTER_PORT}"]  # cpu0 excluded


def test_labels_carry_cluster_scheduler_tenant(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_TENANT", "acme")
    groups = node_targets([_node("remote", "n0", 8, scheduler="slurm")])
    labels = groups[0]["labels"]
    assert (
        labels["cluster"] == "remote" and labels["scheduler"] == "slurm" and labels["tenant"] == "acme"
    )


def test_grouped_per_cluster():
    groups = node_targets([_node("a", "n0", 8), _node("b", "n1", 8)])
    clusters = {g["labels"]["cluster"] for g in groups}
    assert clusters == {"a", "b"}


def test_write_file_sd(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb
    from examlops.prometheus_sd import write_file_sd

    pdb.init_db()
    pdb.record_node_snapshot(
        "remote",
        "flux",
        [
            {"name": "n0", "gpus": 8, "state": "idle"},
            {"name": "n1", "gpus": 0, "state": "idle"},
        ],
    )
    out = tmp_path / "sd.json"
    count = write_file_sd(str(out))
    data = json.loads(out.read_text())
    # 2 node_exporter targets + 1 dcgm target = 3.
    assert count == 3
    assert isinstance(data, list) and all("targets" in g and "labels" in g for g in data)


@pytest.mark.parametrize("gpus,expect_dcgm", [(0, False), (1, True), (8, True)])
def test_dcgm_presence(gpus, expect_dcgm):
    groups = node_targets([_node("c", "n", gpus)])
    has_dcgm = any(g["labels"]["job"] == "dcgm" for g in groups)
    assert has_dcgm is expect_dcgm

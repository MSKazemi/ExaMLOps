"""Prometheus service-discovery generation from the fleet registry (Phase 3 item 3.1).

Fleet-scale node + GPU telemetry needs Prometheus to scrape a `node_exporter` and an NVIDIA
`DCGM-exporter` on every node — but a static `prometheus.yml` can't track thousands of nodes coming
and going. This module generates Prometheus **`file_sd` / `http_sd`** targets directly from the
authoritative `hpc_clusters` / `hpc_nodes` registry, each labelled with cluster/scheduler/tenant, so
adding a node to the registry auto-adds its scrape targets. The exporters themselves are a node-agent
DaemonSet (deployed by the operator); this is the discovery half that wires them to Prometheus.

Pure over injected nodes → testable; the live wrapper reads the registry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

NODE_EXPORTER_PORT = 9100
DCGM_EXPORTER_PORT = 9400


def _tenant() -> str:
    return os.getenv("EXAMLOPS_TENANT", "default")


def node_targets(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build Prometheus ``file_sd`` target groups from registry node rows (item 3.1).

    Each node yields two targets — `node_exporter` (host metrics) and `dcgm` (per-GPU util/power/
    ECC/thermal, only when the node has GPUs) — grouped by (cluster, job) with shared labels so
    queries can slice by cluster/scheduler/tenant.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    tenant = _tenant()
    for n in nodes:
        cluster = n.get("cluster", "default")
        scheduler = n.get("scheduler", "unknown")
        host = n.get("node") or n.get("name")
        if not host:
            continue
        labels = {"cluster": cluster, "scheduler": scheduler, "tenant": tenant}
        # node_exporter for every node.
        key = (cluster, "node")
        grp = groups.setdefault(key, {"targets": [], "labels": {**labels, "job": "node"}})
        grp["targets"].append(f"{host}:{NODE_EXPORTER_PORT}")
        # DCGM only where GPUs exist.
        if (n.get("gpus") or 0) > 0:
            gkey = (cluster, "dcgm")
            ggrp = groups.setdefault(gkey, {"targets": [], "labels": {**labels, "job": "dcgm"}})
            ggrp["targets"].append(f"{host}:{DCGM_EXPORTER_PORT}")
    # Deterministic order for stable file diffs.
    return [groups[k] for k in sorted(groups)]


def generate(cluster: str | None = None) -> list[dict[str, Any]]:
    """Read the registry's node snapshot and build the target groups (live wrapper)."""
    from examlops.data import init_db
    from examlops.data.hpc import get_node_snapshot

    init_db()
    return node_targets(get_node_snapshot(cluster))


def write_file_sd(path: str, cluster: str | None = None) -> int:
    """Write the Prometheus ``file_sd`` JSON to ``path``. Returns the number of targets."""
    groups = generate(cluster)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(groups, indent=2))
    return sum(len(g["targets"]) for g in groups)

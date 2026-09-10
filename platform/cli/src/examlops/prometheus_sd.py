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


def _is_loopback(target: str) -> bool:
    """True for ``host[:port]`` targets that name this machine (``localhost``, ``127.*``, ``::1``)."""
    if target.startswith("[") and "]" in target:  # [v6]:port
        host = target[1 : target.index("]")]
    elif target.count(":") == 1:  # host:port
        host = target.rsplit(":", 1)[0]
    else:  # bare host, or a bare v6 address
        host = target
    host = host.lower()
    return host in ("localhost", "::1", "0.0.0.0") or host.startswith("127.")


def llm_endpoint_targets(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build ``file_sd`` groups for running vLLM endpoints (Track V / ADR 0107).

    A vLLM server exposes ``vllm:*`` metrics (TTFT, inter-token latency, queue depth,
    ``kv_cache_usage_perc``) on its own ``/metrics``. On HPC the server lands on whichever
    node the scheduler allocated, so a static ``prometheus.yml`` entry cannot name it —
    the same problem file_sd already solves for node/DCGM exporters here.

    Only endpoints that are actually up are emitted: scraping a PENDING or STOPPED
    endpoint just manufactures a permanently-down target and a false alert.

    For the same reason two kinds of address are left out. A Compose endpoint is recorded
    at its host-side port (``localhost:18011``), and the static ``vllm`` job already scrapes
    the service by name inside the Compose network. Any loopback address is the Prometheus
    container itself when Prometheus runs in Compose — a target that can never answer, and
    a critical ``VLLMEndpointDown`` for a server that is healthy.
    """
    groups: dict[str, dict[str, Any]] = {}
    tenant = _tenant()
    for ep in endpoints:
        if str(ep.get("state", "")).upper() not in ("READY", "STARTING"):
            continue
        if str(ep.get("launcher", "")).lower() == "compose":
            continue
        base = str(ep.get("base_url") or "")
        target = base.split("://", 1)[-1].split("/", 1)[0]
        if not target or _is_loopback(target):
            continue
        cluster = ep.get("cluster") or "local"
        grp = groups.setdefault(
            cluster,
            {
                "targets": [],
                "labels": {"cluster": cluster, "tenant": tenant, "job": "vllm"},
            },
        )
        grp["targets"].append(target)
    return [groups[k] for k in sorted(groups)]


def generate(cluster: str | None = None, *, include_llm: bool = True) -> list[dict[str, Any]]:
    """Read the registry and build the target groups (live wrapper).

    Covers node/DCGM exporters plus, by default, any running vLLM endpoint — so one
    generated file gives Prometheus the whole fleet's telemetry surface.
    """
    from examlops.data import init_db
    from examlops.data.hpc import get_node_snapshot

    init_db()
    groups = node_targets(get_node_snapshot(cluster))
    if include_llm:
        try:
            from examlops.data.serving import list_llm_endpoints

            groups = groups + llm_endpoint_targets(list_llm_endpoints())
        except Exception:
            # Endpoint discovery is additive — never break node SD generation.
            pass
    return groups


def write_file_sd(path: str, cluster: str | None = None) -> int:
    """Write the Prometheus ``file_sd`` JSON to ``path``. Returns the number of targets."""
    groups = generate(cluster)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(groups, indent=2))
    return sum(len(g["targets"]) for g in groups)

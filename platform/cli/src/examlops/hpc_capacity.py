"""
Per-cluster capacity, utilization and cost — ties discovery + hpc_jobs + FinOps.

Pure, offline-testable helpers that join a cluster's live node inventory (from ``hpc_nodes``)
with its consumed GPU-hours (from ``hpc_jobs``) to answer "are the GPUs online, and are they
being used well?". Cost uses the same ``GPU_COST_PER_HOUR`` default as ``exa models cost``;
carbon accounting is intentionally left to ``exa finops carbon`` (the Green-AI provider
substrate) rather than duplicated here.
"""

from __future__ import annotations

import os

from examlops.hpc_placement import node_capacity


def _gpu_cost_per_hour() -> float:
    try:
        return float(os.getenv("GPU_COST_PER_HOUR", "2.50"))
    except ValueError:
        return 2.50


def gpu_hours_by_scheduler(jobs: list[dict]) -> dict[str, float]:
    """Sum consumed GPU-hours (gpus × run_seconds / 3600) per scheduler from hpc_jobs."""
    out: dict[str, float] = {}
    for j in jobs:
        rs = j.get("run_seconds")
        g = j.get("gpus")
        if rs and g:
            out[j["scheduler"]] = out.get(j["scheduler"], 0.0) + (g * rs / 3600.0)
    return out


def capacity_report(
    clusters: list[dict], jobs: list[dict], gpu_cost_per_hour: float | None = None
) -> list[dict]:
    """Per-cluster capacity + utilization + cost.

    ``clusters`` items: ``{name, scheduler, capabilities, nodes}`` (as from
    ``active_clusters_with_inventory``). ``jobs``: rows from ``get_hpc_jobs``.
    """
    rate = gpu_cost_per_hour if gpu_cost_per_hour is not None else _gpu_cost_per_hour()
    gh = gpu_hours_by_scheduler(jobs)
    rows: list[dict] = []
    for c in clusters:
        cap = node_capacity(c.get("nodes") or [])
        total_gpus = cap["total_gpus"]
        if total_gpus == 0 and c.get("capabilities"):
            total_gpus = c["capabilities"].get("total_gpus", 0) or 0
        used_hours = gh.get(c.get("scheduler") or "", 0.0)
        util = round(100.0 * (total_gpus - cap["idle_gpus"]) / total_gpus, 1) if total_gpus else 0.0
        rows.append(
            {
                "name": c["name"],
                "scheduler": c.get("scheduler"),
                "total_gpus": total_gpus,
                "idle_gpus": cap["idle_gpus"],
                "total_nodes": cap["total_nodes"],
                "idle_nodes": cap["idle_nodes"],
                "utilization_pct": util,
                "gpu_hours_used": round(used_hours, 2),
                "cost_usd": round(used_hours * rate, 2),
            }
        )
    return rows

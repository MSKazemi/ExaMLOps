"""Fleet Digital Twin & What-If Studio — simulation engine (Phase 5 item 5.1, flagship).

A *pure* simulatable model of the fleet: take the live cluster inventory, apply a hypothetical
scenario (add/remove capacity, shift a cluster's grid-carbon intensity or price, or submit a batch of
jobs), and project the outcome — where each job places, how much GPU-hours / cost / carbon it burns,
and how deep the queue gets — **without touching anything real**. It reuses the very same placement
scoring fn (`choose_cluster`) and capacity model the live scheduler uses, so the projection matches
production behaviour. The 3D NOC view (item 5.4) renders this state; this module is the engine.

Everything here is a pure function over an injected cluster list, so it is fully offline-testable;
`simulate()` is the thin adapter that pulls the live inventory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops.hpc_placement import ResourceAsk, _effective_capacity

# Rough energy model for projecting carbon (documented + overridable). A GPU-hour at `GPU_WATTS`
# watts over a PUE-adjusted datacentre draws this many kWh; times grid intensity → gCO₂e.
_GPU_WATTS = 400.0
_PUE = 1.3


@dataclass
class JobSpec:
    gpus: int = 1
    nodes: int = 1
    cpus: int = 0
    duration_h: float = 1.0
    count: int = 1


@dataclass
class Scenario:
    """A hypothetical change to the fleet."""

    add_gpus: dict[str, int] = field(default_factory=dict)  # cluster -> extra idle GPUs
    carbon_overrides: dict[str, float] = field(default_factory=dict)  # cluster -> gCO₂e/kWh
    cost_overrides: dict[str, float] = field(default_factory=dict)  # cluster -> $/gpu-h
    jobs: list[JobSpec] = field(default_factory=list)


def _clone_with_scenario(clusters: list[dict], scenario: Scenario) -> list[dict]:
    """Return a deep-enough copy of ``clusters`` with the scenario's capacity/price/carbon applied."""
    out: list[dict] = []
    for c in clusters:
        caps = dict(c.get("capabilities") or {})
        name = c["name"]
        if name in scenario.carbon_overrides:
            caps["carbon_intensity"] = scenario.carbon_overrides[name]
        if name in scenario.cost_overrides:
            caps["cost_per_gpu_hour"] = scenario.cost_overrides[name]
        # Fold added GPUs into declared capability totals (the twin works on effective capacity).
        eff = _effective_capacity({**c, "capabilities": caps})
        extra = scenario.add_gpus.get(name, 0)
        caps["total_gpus"] = eff.get("total_gpus", 0) + extra
        caps["total_nodes"] = eff.get("total_nodes", 0)
        # Model added GPUs as idle by carrying idle counts on the working copy.
        out.append(
            {
                "name": name,
                "scheduler": c.get("scheduler"),
                "capabilities": caps,
                "nodes": [],  # simulate from declared capacity, not the live node list
                "_idle_gpus": eff.get("idle_gpus", 0) + extra,
                "_idle_nodes": eff.get("idle_nodes", 0),
            }
        )
    return out


def project(clusters: list[dict], scenario: Scenario, *, score_fn=None) -> dict[str, Any]:
    """Project the scenario's outcome over ``clusters``. Pure — mutates only a local copy.

    Places each job (respecting the scenario's added capacity) greedily by the placement score,
    decrementing idle capacity as it goes, and rolls up utilization / GPU-hours / cost / carbon /
    queue depth. Jobs that can't be placed land in the projected queue.
    """
    from examlops.hpc_placement import headroom_score

    score = score_fn or headroom_score
    working = _clone_with_scenario(clusters, scenario)

    placements: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    gpu_hours = cost_usd = carbon_g = 0.0

    # Expand job specs (count) into individual asks.
    asks: list[JobSpec] = []
    for j in scenario.jobs:
        asks.extend(
            [JobSpec(j.gpus, j.nodes, j.cpus, j.duration_h) for _ in range(max(1, j.count))]
        )

    for idx, job in enumerate(asks):
        ask = ResourceAsk(gpus=job.gpus, cpus=job.cpus, nodes=job.nodes)
        # Place directly against the twin's LIVE idle tracking (so capacity actually depletes across
        # jobs — choose_cluster would recompute effective capacity from the empty node list and reset
        # idle to full). Score every cluster that currently fits; pick the best.
        best_cluster = None
        best_score = float("-inf")
        for c in working:
            if c["_idle_gpus"] < job.gpus or c["_idle_nodes"] < job.nodes:
                continue
            cap = {
                "idle_gpus": c["_idle_gpus"],
                "idle_nodes": c["_idle_nodes"],
                "total_gpus": c["capabilities"].get("total_gpus", 0),
                "total_nodes": c["capabilities"].get("total_nodes", 0),
                "idle_cpus": 0,
                **{
                    k: v
                    for k, v in c["capabilities"].items()
                    if k in ("carbon_intensity", "cost_per_gpu_hour")
                },
            }
            s = score(ask, cap)
            if s > best_score:
                best_score, best_cluster = s, c
        if best_cluster is None:
            queued.append({"job": idx, "gpus": job.gpus})
            continue
        chosen = best_cluster
        chosen["_idle_gpus"] -= job.gpus
        chosen["_idle_nodes"] = max(0, chosen["_idle_nodes"] - job.nodes)
        gh = job.gpus * job.duration_h
        gpu_hours += gh
        rate = chosen["capabilities"].get("cost_per_gpu_hour")
        if rate is not None:
            cost_usd += gh * float(rate)
        ci = chosen["capabilities"].get("carbon_intensity")
        if ci is not None:
            kwh = gh * _GPU_WATTS * _PUE / 1000.0
            carbon_g += kwh * float(ci)
        placements.append({"job": idx, "cluster": chosen["name"], "gpus": job.gpus})

    return {
        "clusters": [
            {
                "name": c["name"],
                "idle_gpus": c["_idle_gpus"],
                "total_gpus": c["capabilities"].get("total_gpus", 0),
            }
            for c in working
        ],
        "placed": len(placements),
        "queued": len(queued),
        "placements": placements,
        "queue_depth": len(queued),
        "projected_gpu_hours": round(gpu_hours, 2),
        "projected_cost_usd": round(cost_usd, 2),
        "projected_carbon_kg": round(carbon_g / 1000.0, 3),
    }


def simulate(scenario: Scenario, *, score_fn=None) -> dict[str, Any]:
    """Pull the live ACTIVE-cluster inventory and project ``scenario`` over it (item 5.1)."""
    from examlops.hpc_registry import active_clusters_with_inventory

    clusters = active_clusters_with_inventory()
    baseline = project(clusters, Scenario(), score_fn=score_fn)
    projected = project(clusters, scenario, score_fn=score_fn)
    return {
        "baseline": baseline,
        "projected": projected,
        "delta": {
            "gpu_hours": round(
                projected["projected_gpu_hours"] - baseline["projected_gpu_hours"], 2
            ),
            "cost_usd": round(projected["projected_cost_usd"] - baseline["projected_cost_usd"], 2),
            "carbon_kg": round(
                projected["projected_carbon_kg"] - baseline["projected_carbon_kg"], 3
            ),
            "queue_depth": projected["queue_depth"] - baseline["queue_depth"],
        },
    }

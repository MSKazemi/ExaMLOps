"""
Scheduler-aware placement — pick the best ACTIVE cluster for a training job.

Given a job's resource ask (GPUs / CPUs / nodes) and the live inventory of each approved
cluster (node snapshots from ``hpc_nodes``, or the declared capabilities when no snapshot
exists), choose the cluster with the most matching headroom and explain *why*. Pure and
offline-testable: it takes plain dicts and returns a decision — the CLI does the I/O
(reading the registry, resolving env, launching the run).

Policies are intentionally simple and explainable (has-capacity → least-loaded). The
scoring is a single function so a future policy (cost-aware, carbon-aware, fair-share) can
replace :func:`headroom_score` without touching the selection loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# A scorer maps (ask, capacity-dict) → a float; higher = better placement. The built-in default is
# :func:`headroom_score`; a pluggable provider can supply another (ADR 0077) without editing this
# module, which stays pure/offline (the caller resolves the provider and injects the callable).
ScoreFn = Callable[["ResourceAsk", dict], float]


@dataclass
class ResourceAsk:
    gpus: int = 0
    cpus: int = 0
    nodes: int = 1


@dataclass
class PlacementResult:
    cluster: str | None
    reason: str
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"cluster": self.cluster, "reason": self.reason, "candidates": self.candidates}


def node_capacity(nodes: list[dict]) -> dict:
    """Aggregate a cluster's node list into capacity totals (idle vs total)."""
    idle = [n for n in nodes if (n.get("state") == "idle")]
    return {
        "total_nodes": len(nodes),
        "idle_nodes": len(idle),
        "total_gpus": sum((n.get("gpus") or 0) for n in nodes),
        "idle_gpus": sum((n.get("gpus") or 0) for n in idle),
        "idle_cpus": sum((n.get("cpus") or 0) for n in idle),
    }


def _effective_capacity(cluster: dict) -> dict:
    """Capacity from the node snapshot, falling back to declared capabilities.

    Also passes through any *extra scalar* capability fields (e.g. ``carbon_intensity``,
    ``cost_per_gpu_hour``) that aren't part of the standard capacity keys, so a pluggable placement
    formula (ADR 0077) can score on them — carbon-/cost-aware placement with no core change.
    """
    caps = cluster.get("capabilities") or {}
    cap = node_capacity(cluster.get("nodes") or [])
    if cap["total_nodes"] == 0 and caps:
        cap["total_gpus"] = caps.get("total_gpus", 0) or 0
        cap["total_nodes"] = caps.get("total_nodes", 0) or 0
        # No live state — assume declared capacity is available.
        cap["idle_gpus"] = cap["total_gpus"]
        cap["idle_nodes"] = cap["total_nodes"]
    for key, value in caps.items():
        if key not in cap and isinstance(value, (int, float)) and not isinstance(value, bool):
            cap[key] = value
    return cap


def can_satisfy(ask: ResourceAsk, cap: dict) -> bool:
    """Can this cluster ever host the ask (by total capacity, not just idle)?"""
    if ask.gpus > 0 and cap["total_gpus"] < ask.gpus:
        return False
    if ask.nodes > 0 and cap["total_nodes"] < ask.nodes:
        return False
    return True


def headroom_score(ask: ResourceAsk, cap: dict) -> float:
    """Higher = more idle headroom after satisfying the ask (GPUs weighted heaviest)."""
    return (cap["idle_gpus"] - ask.gpus) * 100 + (cap["idle_nodes"] - ask.nodes)


def choose_cluster(
    ask: ResourceAsk, clusters: list[dict], score_fn: ScoreFn | None = None
) -> PlacementResult:
    """Choose the best ACTIVE cluster that can satisfy ``ask`` under a scoring policy.

    ``clusters`` items: ``{name, scheduler, capabilities: {...}|None, nodes: [ {...} ]}``.
    ``score_fn`` is the placement policy: it maps ``(ask, capacity)`` → a float (higher = better).
    When ``None`` it defaults to :func:`headroom_score` (least-loaded), so behaviour is unchanged
    unless a caller injects a pluggable provider's scorer (ADR 0077). Returns a
    :class:`PlacementResult` with the chosen cluster (or ``None``) plus a scored, human-readable
    candidate list for transparency.
    """
    score = score_fn or headroom_score
    candidates: list[dict] = []
    for c in clusters:
        cap = _effective_capacity(c)
        fits = can_satisfy(ask, cap)
        candidates.append(
            {
                "name": c["name"],
                "scheduler": c.get("scheduler"),
                "fits": fits,
                "score": score(ask, cap) if fits else float("-inf"),
                "idle_gpus": cap["idle_gpus"],
                "total_gpus": cap["total_gpus"],
                "idle_nodes": cap["idle_nodes"],
                "total_nodes": cap["total_nodes"],
            }
        )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    fitting = [c for c in candidates if c["fits"]]
    if not fitting:
        if not clusters:
            reason = "no ACTIVE clusters registered — 'exa hpc connect' then 'exa hpc approve'"
        else:
            reason = f"no ACTIVE cluster can satisfy the ask (gpus={ask.gpus}, nodes={ask.nodes})"
        return PlacementResult(None, reason, candidates)

    best = fitting[0]
    reason = (
        f"chose {best['name']} ({best['scheduler']}) — "
        f"{best['idle_gpus']}/{best['total_gpus']} idle GPUs, "
        f"{best['idle_nodes']}/{best['total_nodes']} idle nodes"
    )
    return PlacementResult(best["name"], reason, candidates)

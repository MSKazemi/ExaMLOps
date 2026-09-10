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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only; this module stays pure and import-light
    from examlops.finops.carbon_signal import CarbonSignal

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
    #: Objectives that could not be scored, named rather than silently dropped (ADR 0112
    #: decision 5). An operator reading a placement must be able to see that carbon played no
    #: part in it; a zero weight that says nothing is indistinguishable from a bug.
    objectives_unavailable: list[str] = field(default_factory=list)
    #: What each scored objective contributed, for the same reason.
    objectives: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "cluster": self.cluster,
            "reason": self.reason,
            "candidates": self.candidates,
            "objectives_unavailable": self.objectives_unavailable,
            "objectives": self.objectives,
        }


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


def carbon_objective_state(signal: CarbonSignal | None) -> tuple[bool, str]:
    """Whether carbon may weigh on a placement, and why (ADR 0112 decisions 3–5).

    Returns ``(usable, reason)``. Only a **decision** (marginal/consequential) signal may drive
    placement: shifting on an *average* signal is the documented way to reduce the emissions
    allocated to you while increasing the power system's total. When no decision signal exists
    the carbon objective's weight is **zero and recorded** — no default is substituted, because
    substituting one here is the harm itself, not a convenience.
    """
    if signal is None:
        return False, "no carbon signal available"
    if not signal.is_decision:
        return False, (
            f"carbon signal is '{signal.signal_type}' (method={signal.method!r}); "
            "placement needs a decision/marginal signal"
        )
    return True, f"decision signal via {signal.method}"


def choose_cluster(
    ask: ResourceAsk,
    clusters: list[dict],
    score_fn: ScoreFn | None = None,
    *,
    carbon_signal: CarbonSignal | None = None,
    strict_carbon: bool = False,
) -> PlacementResult:
    """Choose the best ACTIVE cluster that can satisfy ``ask`` under a scoring policy.

    ``clusters`` items: ``{name, scheduler, capabilities: {...}|None, nodes: [ {...} ]}``.
    ``score_fn`` is the placement policy: it maps ``(ask, capacity)`` → a float (higher = better).
    When ``None`` it defaults to :func:`headroom_score` (least-loaded), so behaviour is unchanged
    unless a caller injects a pluggable provider's scorer (ADR 0077). Returns a
    :class:`PlacementResult` with the chosen cluster (or ``None``) plus a scored, human-readable
    candidate list for transparency.

    ``carbon_signal`` is checked, never assumed (ADR 0112). A usable **decision** signal is
    published to each capacity dict as ``carbon_intensity_decision`` so a provider's scorer can
    weigh it; anything else leaves the carbon objective unscored and named in
    ``objectives_unavailable``. ``strict_carbon=True`` makes a wrong-typed signal **raise**
    instead — for a caller that asked for carbon-aware placement and must not silently get
    carbon-blind placement.
    """
    score = score_fn or headroom_score
    objectives_unavailable: list[str] = []
    objectives: dict = {}
    policy = getattr(score, "placement_policy", None)  # ADR 0112 R-ec gate outcome, if any
    if policy:
        objectives["placement_policy"] = policy
    carbon_usable, carbon_reason = carbon_objective_state(carbon_signal)
    if carbon_usable and carbon_signal is not None:
        objectives["carbon"] = carbon_signal.as_dict()
    else:
        if strict_carbon:
            from examlops.finops.carbon_signal import CarbonSignalTypeError

            raise CarbonSignalTypeError(f"carbon-aware placement requested but {carbon_reason}")
        objectives_unavailable.append("carbon")
        objectives["carbon"] = {"weight": 0.0, "reason": carbon_reason}

    candidates: list[dict] = []
    for c in clusters:
        cap = _effective_capacity(c)
        if carbon_usable and carbon_signal is not None:
            cap["carbon_intensity_decision"] = carbon_signal.grams_per_kwh
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
        return PlacementResult(None, reason, candidates, objectives_unavailable, objectives)

    best = fitting[0]
    reason = (
        f"chose {best['name']} ({best['scheduler']}) — "
        f"{best['idle_gpus']}/{best['total_gpus']} idle GPUs, "
        f"{best['idle_nodes']}/{best['total_nodes']} idle nodes"
    )
    if policy and policy.get("action") != "allow":
        # ADR 0112 R-ec/R-ed: say which policy actually placed the job, and why it is not the
        # one that was asked for — a silent substitution would read as the requested policy.
        reason += (
            f" [placement policy: {policy['requested']} → {policy['effective']}"
            f"{'' if policy.get('carbon_input') else ', carbon input withheld'}: {policy['reason']}]"
        )
    if objectives_unavailable:
        reason += (
            f" [objectives unavailable: {', '.join(objectives_unavailable)} — {carbon_reason}]"
        )
    return PlacementResult(best["name"], reason, candidates, objectives_unavailable, objectives)

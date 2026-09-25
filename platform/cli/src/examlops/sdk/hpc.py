"""``examlops.hpc`` — the HPC fleet through the stable SDK (ADR 0078 clause 1).

``place`` recommends a cluster (the pluggable placement provider, ADR 0077); ``clusters`` lists
the registry with each cluster's approval state; ``capacity`` reports GPU capacity, utilisation,
GPU-hours and cost of the ACTIVE clusters. All read-only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from examlops.sdk.errors import UnavailableError

if TYPE_CHECKING:
    from examlops.hpc_placement import PlacementResult

__all__ = ["Cluster", "ClusterCapacity", "place", "clusters", "capacity"]


@dataclass(frozen=True)
class Cluster:
    """One registered cluster: its connection definition and governance state."""

    name: str
    state: str
    scheduler: str | None = None
    transport: str | None = None
    host: str | None = None
    approved_by: str | None = None
    requested_by: str | None = None
    reason: str | None = None
    capabilities: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The historical ``exa --json hpc clusters`` row."""
        return dict(self.raw)


@dataclass(frozen=True)
class ClusterCapacity:
    """Capacity, utilisation and spend of one ACTIVE cluster."""

    name: str
    scheduler: str | None
    total_gpus: int
    idle_gpus: int
    total_nodes: int
    idle_nodes: int
    utilization_pct: float
    gpu_hours_used: float
    cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        """The historical ``exa --json hpc capacity`` row (same keys, same order)."""
        return asdict(self)


def place(
    gpus: int = 0, cpus: int = 0, nodes: int = 1, provider: str | None = None
) -> PlacementResult:
    """Recommend which ACTIVE cluster should run a job — same as :func:`examlops.sdk.place`."""
    from examlops.sdk import place as _place

    return _place(gpus=gpus, cpus=cpus, nodes=nodes, provider=provider)


def clusters() -> list[Cluster]:
    """Every registered cluster (``clusters.yaml`` ∪ the registry) with its approval state."""
    from examlops.hpc_registry import list_clusters

    try:
        rows = list_clusters()
    except Exception as exc:  # noqa: BLE001 - registry/datastore errors are private types
        raise UnavailableError(f"cluster registry unavailable: {exc}") from exc
    return [
        Cluster(
            name=str(r["name"]),
            state=str(r.get("state") or "PENDING"),
            scheduler=r.get("scheduler"),
            transport=r.get("transport"),
            host=r.get("host"),
            approved_by=r.get("approved_by"),
            requested_by=r.get("requested_by"),
            reason=r.get("reason"),
            capabilities=r.get("capabilities"),
            raw=dict(r),
        )
        for r in rows
    ]


def capacity() -> list[ClusterCapacity]:
    """GPU capacity/utilisation/GPU-hours/cost per ACTIVE cluster (carbon: ``exa finops``)."""
    from examlops.data import init_db
    from examlops.data.hpc import get_hpc_jobs
    from examlops.hpc_capacity import capacity_report
    from examlops.hpc_registry import active_clusters_with_inventory

    try:
        init_db()
        rows = capacity_report(active_clusters_with_inventory(), get_hpc_jobs())
    except Exception as exc:  # noqa: BLE001 - registry/datastore errors are private types
        raise UnavailableError(f"fleet capacity unavailable: {exc}") from exc
    return [
        ClusterCapacity(
            name=str(r["name"]),
            scheduler=r.get("scheduler"),
            total_gpus=int(r["total_gpus"]),
            idle_gpus=int(r["idle_gpus"]),
            total_nodes=int(r["total_nodes"]),
            idle_nodes=int(r["idle_nodes"]),
            utilization_pct=r["utilization_pct"],
            gpu_hours_used=r["gpu_hours_used"],
            cost_usd=r["cost_usd"],
        )
        for r in rows
    ]

"""Render Kueue manifests from quotas (ADR 0116 decision 5, 7) - generator output only.

Pure: it returns manifest dicts and applies nothing (like ``exa serve manifest``). It is NOT a
Kueue adapter. What is unbuilt, on purpose and stated here so the output cannot be mistaken for
more than it is:

* the ``AdmissionCheck`` names a controller (``examlops.io/admission``) that does not exist in
  this repository - the rendered check would sit ``Pending`` forever if applied;
* no ``SchedulerAdapter`` for Kubernetes exists, so nothing submits a Workload;
* the manifests were not validated against a live Kueue (``KUEUE_API_VERSION`` is the version the
  shape was written for).

A request it cannot express faithfully is *refused* (:class:`KueueUnsupported`), never rendered
as an approximation: non-Kubernetes clusters (Slurm/Flux are not Kueue-fronted), a non-default
over-quota weight (needs Kueue's FairSharing feature gate), and gang / topology requirements.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .policy import Quotas

KUEUE_API_VERSION = "kueue.x-k8s.io/v1beta1"
ADMISSION_CONTROLLER = "examlops.io/admission"
GPU_RESOURCE = "nvidia.com/gpu"
FLAVOR_LABEL = "examlops.io/gpu-flavor"


class KueueUnsupported(ValueError):
    """The input needs something this generator will not approximate."""


@dataclass(frozen=True)
class FlavorSpec:
    name: str
    gpus: int
    gpu_resource: str = GPU_RESOURCE


def flavors_from_clusters(clusters: Sequence[Mapping[str, Any]]) -> list[FlavorSpec]:
    """One flavor per registered cluster; a cluster Kueue cannot front is refused."""
    out: list[FlavorSpec] = []
    for c in clusters:
        sched = str(c.get("scheduler") or "").lower()
        if sched != "kubernetes":
            raise KueueUnsupported(
                f"cluster {c.get('name')!r} runs {sched or 'an unknown scheduler'!r}; Kueue only "
                "fronts Kubernetes clusters (Slurm and Flux keep their own admission)"
            )
        caps = c.get("capabilities") or {}
        out.append(FlavorSpec(str(c["name"]), int(caps.get("total_gpus") or 0)))
    return out


def render_kueue(
    quotas: Quotas,
    flavors: Sequence[FlavorSpec],
    *,
    projects_by_tenant: Mapping[str, Sequence[str]] | None = None,
    cohort: str = "examlops",
    admission_check: bool = True,
    require_gang: bool = False,
    require_topology: bool = False,
) -> list[dict[str, Any]]:
    if require_gang or require_topology:
        raise KueueUnsupported(
            "gang / topology-aware admission needs Kueue's TopologyAwareScheduling and "
            "Workload podset wiring, which this generator does not render"
        )
    if not flavors:
        raise KueueUnsupported("no ResourceFlavor: give at least one Kubernetes cluster")
    if len(flavors) > 1:
        raise KueueUnsupported(
            "several flavors: how a tenant's single GPU baseline splits across them is not "
            "defined by ADR 0116, so it is not guessed"
        )
    if not quotas.tenants:
        raise KueueUnsupported("no per-tenant quotas configured; nothing to render a queue for")
    for name, q in quotas.tenants.items():
        if q.over_quota_weight != 1.0:
            raise KueueUnsupported(
                f"tenant {name!r} has over_quota_weight {q.over_quota_weight}; weighted "
                "over-quota sharing needs Kueue FairSharing, which is not rendered"
            )
    docs: list[dict[str, Any]] = []
    for f in flavors:
        docs.append(
            {
                "apiVersion": KUEUE_API_VERSION,
                "kind": "ResourceFlavor",
                "metadata": {"name": f.name},
                "spec": {"nodeLabels": {FLAVOR_LABEL: f.name}},
            }
        )
    if admission_check:
        docs.append(
            {
                "apiVersion": KUEUE_API_VERSION,
                "kind": "AdmissionCheck",
                "metadata": {"name": "examlops-admission"},
                "spec": {"controllerName": ADMISSION_CONTROLLER},
            }
        )
    for tenant, q in sorted(quotas.tenants.items()):
        flavor_entries = []
        for f in flavors:
            entry: dict[str, Any] = {
                "name": f.name,
                "resources": [
                    {
                        "name": f.gpu_resource,
                        "nominalQuota": q.baseline_gpus,
                        **(
                            {"borrowingLimit": max(0, q.limit_gpus - q.baseline_gpus)}
                            if q.limit_gpus is not None
                            else {}
                        ),
                    }
                ],
            }
            flavor_entries.append(entry)
        spec: dict[str, Any] = {
            "cohort": cohort,
            "namespaceSelector": {},
            "resourceGroups": [
                {"coveredResources": [flavors[0].gpu_resource], "flavors": flavor_entries}
            ],
            # No reclamation or preemption is promised (ADR 0116 status): say Never explicitly.
            "preemption": {"withinClusterQueue": "Never", "reclaimWithinCohort": "Never"},
        }
        if admission_check:
            spec["admissionChecks"] = ["examlops-admission"]
        docs.append(
            {
                "apiVersion": KUEUE_API_VERSION,
                "kind": "ClusterQueue",
                "metadata": {"name": tenant},
                "spec": spec,
            }
        )
        for project in (projects_by_tenant or {}).get(tenant, []):
            docs.append(
                {
                    "apiVersion": KUEUE_API_VERSION,
                    "kind": "LocalQueue",
                    "metadata": {"name": project, "namespace": project},
                    "spec": {"clusterQueue": tenant},
                }
            )
    return docs

"""examlops.hardware_profiles — Hardware Profiles: named, versioned resource+runtime bundles.

ADR 0157, spec `design/vision/specs/spec-hardware-profiles.md` §2.2. Pure logic (no direct
database access): resolution against live cluster capacity, plus thin adapters into the three
resource-ask shapes this repo already has —
:class:`examlops.admission_seam.request.Resources`, :class:`examlops.hpc_placement.ResourceAsk`
and :class:`examlops.hardware.Workload`. Persistence lives in
:mod:`examlops.data.hardware_profiles`, mirroring the existing ``hpc_placement.py`` /
``data/hpc.py`` split (ADR 0157 decision 1). A profile is sugar over the existing seams — never
a fourth, parallel resource vocabulary (ADR 0157 §Decision item 3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from examlops import hpc_placement
from examlops.admission_seam.request import Resources
from examlops.data import hardware_profiles as _data
from examlops.data import hpc as _hpc
from examlops.hardware import ACCELERATORS, Workload

#: Subset of these an ``applicability`` tuple may contain (spec §2).
APPLICABILITIES = ("workbench", "training", "serving", "any")

#: :attr:`ProfileResolution.status` values (ADR 0157 decision 5).
STATUS_UNCHECKED = "unchecked"
STATUS_VERIFIED = "verified"
STATUS_DEGRADED = "degraded"
STATUS_UNRESOLVABLE = "unresolvable"

__all__ = [
    "APPLICABILITIES",
    "STATUS_DEGRADED",
    "STATUS_UNCHECKED",
    "STATUS_UNRESOLVABLE",
    "STATUS_VERIFIED",
    "HardwareProfile",
    "HardwareProfileError",
    "ProfileResolution",
    "create_profile_version",
    "get_profile",
    "list_names",
    "list_versions",
    "resolve_profile",
    "to_resource_ask",
    "to_workload",
]


class HardwareProfileError(ValueError):
    """A hardware-profile request that is invalid, missing, or cannot be resolved."""


@dataclass(frozen=True)
class HardwareProfile:
    """One immutable version of a named profile (spec §2)."""

    name: str
    version: int
    accelerator_family: str
    gpu_count: int = 0
    gpu_fraction: float = 1.0
    mig_profile: str | None = None
    cpu: float = 0.0
    memory_gb: float = 0.0
    nodes: int = 1
    accelerator_model_hint: str | None = None
    driver_tag: str | None = None
    runtime_tag: str | None = None
    applicability: tuple[str, ...] = ("any",)
    description: str = ""
    created_at: str | None = None
    created_by: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> HardwareProfile:
        raw = (row.get("applicability") or "").strip()
        applicability = tuple(a for a in raw.split(",") if a) or ("any",)
        return cls(
            name=row["name"],
            version=int(row["version"]),
            accelerator_family=row["accelerator_family"],
            gpu_count=int(row.get("gpu_count") or 0),
            gpu_fraction=float(row["gpu_fraction"] if row.get("gpu_fraction") is not None else 1.0),
            mig_profile=row.get("mig_profile"),
            cpu=float(row.get("cpu") or 0.0),
            memory_gb=float(row.get("memory_gb") or 0.0),
            nodes=int(row.get("nodes") or 1),
            accelerator_model_hint=row.get("accelerator_model_hint"),
            driver_tag=row.get("driver_tag"),
            runtime_tag=row.get("runtime_tag"),
            applicability=applicability,
            description=row.get("description") or "",
            created_at=row.get("created_at"),
            created_by=row.get("created_by"),
        )


@dataclass(frozen=True)
class ProfileResolution:
    """The outcome of resolving a profile at the point of use (spec §2.2)."""

    name: str
    version: int
    status: str  # unchecked | verified | degraded | unresolvable
    reason: str
    resources: Resources
    #: Field names not confirmed by live discovery (only set when ``status == "degraded"``).
    unconfirmed: tuple[str, ...] = ()


def _validate_applicability(applicability: tuple[str, ...]) -> None:
    if not applicability:
        raise HardwareProfileError("applicability must not be empty")
    bad = sorted(set(applicability) - set(APPLICABILITIES))
    if bad:
        raise HardwareProfileError(f"applicability {bad} not in {list(APPLICABILITIES)}")


def create_profile_version(
    name: str,
    *,
    accelerator_family: str,
    gpu_count: int = 0,
    gpu_fraction: float = 1.0,
    mig_profile: str | None = None,
    cpu: float = 0.0,
    memory_gb: float = 0.0,
    nodes: int = 1,
    accelerator_model_hint: str | None = None,
    driver_tag: str | None = None,
    runtime_tag: str | None = None,
    applicability: tuple[str, ...] = ("any",),
    description: str = "",
    label: str = "active",
    created_by: str | None = None,
) -> HardwareProfile:
    """Create a new immutable version and move ``label`` (default ``active``) to it.

    Validates ``accelerator_family`` against :data:`examlops.hardware.ACCELERATORS` — no second
    accelerator enum (ADR 0157 decision 1) — and ``applicability`` against
    :data:`APPLICABILITIES`, rejecting an empty tuple (spec §2: "``set()`` MUST reject an empty
    tuple").
    """
    if accelerator_family not in ACCELERATORS:
        raise HardwareProfileError(
            f"accelerator_family must be one of {ACCELERATORS}, got {accelerator_family!r}"
        )
    _validate_applicability(applicability)
    if not (0.0 < gpu_fraction <= 1.0):
        raise HardwareProfileError(f"gpu_fraction must be in (0.0, 1.0], got {gpu_fraction!r}")
    version = _data.create_profile_version(
        name,
        accelerator_family=accelerator_family,
        gpu_count=gpu_count,
        gpu_fraction=gpu_fraction,
        mig_profile=mig_profile,
        cpu=cpu,
        memory_gb=memory_gb,
        nodes=nodes,
        accelerator_model_hint=accelerator_model_hint,
        driver_tag=driver_tag,
        runtime_tag=runtime_tag,
        applicability=applicability,
        description=description,
        created_by=created_by,
    )
    _data.set_profile_label(name, label, version)
    row = _data.get_profile_version(name, version)
    assert row is not None  # just written, in the same call
    return HardwareProfile.from_row(row)


def get_profile(
    name: str, *, label: str = "active", version: int | None = None
) -> HardwareProfile | None:
    """Resolve a profile by explicit ``version``, else by ``label`` (default ``active``)."""
    row = (
        _data.get_profile_version(name, version)
        if version is not None
        else _data.resolve_label(name, label)
    )
    return HardwareProfile.from_row(row) if row else None


def list_versions(name: str) -> list[HardwareProfile]:
    """Every version of ``name``, newest first."""
    return [HardwareProfile.from_row(r) for r in _data.list_profile_versions(name)]


def list_names() -> list[str]:
    """Every profile name that has at least one version."""
    return _data.list_profile_names()


def _capacity_for(cluster: dict | None, snapshot: list[dict]) -> dict:
    """The same capacity dict shape ``hpc_placement.choose_cluster`` scores placements against.

    Reuses :func:`examlops.hpc_placement._effective_capacity` exactly — it already falls back
    from a live node snapshot to a cluster's declared ``capabilities`` when no snapshot exists,
    which is the "node_capacity falling back to _effective_capacity" behaviour spec §2.2 step 2
    describes (``choose_cluster`` does the same per-candidate).
    """
    caps = (cluster or {}).get("capabilities")
    if isinstance(caps, str):
        try:
            caps = json.loads(caps) if caps else None
        except (TypeError, ValueError):
            caps = None
    return hpc_placement._effective_capacity({"capabilities": caps, "nodes": snapshot})


def resolve_profile(
    name: str,
    *,
    label: str = "active",
    version: int | None = None,
    target_cluster: str | None = None,
) -> ProfileResolution:
    """Resolve a hardware profile into a scheduler-neutral ask (ADR 0157 decision 5, spec §2.2).

    Never fabricates a capability it cannot confirm:

    1. No ``target_cluster`` -> ``unchecked``.
    2. A target is given but its (possibly empty) capacity cannot satisfy the coarse ask
       (gpus/nodes, via :func:`hpc_placement.can_satisfy`) -> ``unresolvable``.
    3. Capacity is satisfiable but ``accelerator_model_hint``/``mig_profile`` cannot be
       confirmed from what discovery reported for that cluster -> ``degraded``.
    4. Otherwise -> ``verified``.
    """
    profile = get_profile(name, label=label, version=version)
    if profile is None:
        ref = f"version {version}" if version is not None else f"label {label!r}"
        raise HardwareProfileError(f"hardware profile {name!r} ({ref}) not found")

    resources = Resources(
        gpus=profile.gpu_count,
        cpus=int(profile.cpu),
        memory_gb=profile.memory_gb,
        nodes=profile.nodes,
    )

    if target_cluster is None:
        return ProfileResolution(
            name=profile.name,
            version=profile.version,
            status=STATUS_UNCHECKED,
            reason="no target cluster given",
            resources=resources,
        )

    cluster = _hpc.get_cluster(target_cluster)
    snapshot = _hpc.get_node_snapshot(target_cluster)
    cap = _capacity_for(cluster, snapshot)

    ask = hpc_placement.ResourceAsk(
        gpus=profile.gpu_count, cpus=int(profile.cpu), nodes=profile.nodes
    )
    if not hpc_placement.can_satisfy(ask, cap):
        exceeded = []
        if ask.gpus > 0 and cap["total_gpus"] < ask.gpus:
            exceeded.append(f"gpus (want {ask.gpus}, cluster total {cap['total_gpus']})")
        if ask.nodes > 0 and cap["total_nodes"] < ask.nodes:
            exceeded.append(f"nodes (want {ask.nodes}, cluster total {cap['total_nodes']})")
        reason = f"{target_cluster!r} cannot satisfy the ask: " + (
            "; ".join(exceeded) if exceeded else "no capacity reported"
        )
        return ProfileResolution(
            name=profile.name,
            version=profile.version,
            status=STATUS_UNRESOLVABLE,
            reason=reason,
            resources=resources,
        )

    unconfirmed: list[str] = []
    if profile.accelerator_model_hint:
        models_seen = {n.get("gpu_model") for n in snapshot if n.get("gpu_model")}
        if profile.accelerator_model_hint not in models_seen:
            unconfirmed.append("accelerator_model_hint")
    if profile.mig_profile:
        mig_profiles: list = []
        caps_json = (cluster or {}).get("capabilities")
        if caps_json:
            try:
                parsed = json.loads(caps_json) if isinstance(caps_json, str) else caps_json
                mig_profiles = (parsed or {}).get("mig_profiles") or []
            except (TypeError, ValueError):
                mig_profiles = []
        if profile.mig_profile not in mig_profiles:
            unconfirmed.append("mig_profile")

    if unconfirmed:
        probe = (cluster or {}).get("scheduler") or "the registered"
        reason = (
            f"{probe} probe for {target_cluster!r} does not report "
            f"{', '.join(unconfirmed)} — the coarse ask (gpus/cpu/nodes) is satisfiable, but "
            "this finer claim is unconfirmed"
        )
        return ProfileResolution(
            name=profile.name,
            version=profile.version,
            status=STATUS_DEGRADED,
            reason=reason,
            resources=resources,
            unconfirmed=tuple(unconfirmed),
        )

    return ProfileResolution(
        name=profile.name,
        version=profile.version,
        status=STATUS_VERIFIED,
        reason=f"{target_cluster!r} snapshot satisfies the ask and confirms every named field",
        resources=resources,
    )


def to_resource_ask(resolution: ProfileResolution) -> hpc_placement.ResourceAsk:
    """Adapt a resolution into the placement seam's ask (used by ``--cluster auto``)."""
    r = resolution.resources
    return hpc_placement.ResourceAsk(gpus=r.gpus, cpus=r.cpus, nodes=r.nodes)


def to_workload(resolution: ProfileResolution, workload_name: str) -> Workload:
    """Adapt a resolution into an :class:`examlops.hardware.Workload` (``exa hardware place``).

    ``ProfileResolution`` intentionally does not carry ``accelerator_family``/``gpu_fraction``
    (spec §2.2) — it re-reads the exact version it resolved, so the workload it builds is never
    silently drawn from a different (e.g. since-moved ``active``) version.
    """
    profile = get_profile(resolution.name, version=resolution.version)
    if profile is None:
        raise HardwareProfileError(
            f"hardware profile {resolution.name!r} version {resolution.version} no longer exists"
        )
    return Workload(
        workload_name,
        accelerator=profile.accelerator_family,
        fraction=profile.gpu_fraction,
    )

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
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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

#: Report-only status (never a resolution result): a consumer is still bound to a profile version
#: that has since been deleted, so nothing can say what shape it runs with (ADR 0157 Phase 4).
STATUS_MISSING = "missing"

#: Statuses an operator must look at — surfaced by ``exa status`` and the dashboard.
ATTENTION_STATUSES = frozenset({STATUS_DEGRADED, STATUS_UNRESOLVABLE, STATUS_MISSING})

#: Who resolves a profile — the three consumers ADR 0157 wires, one per applicability.
CONSUMERS = ("workbench", "training", "serving")

logger = logging.getLogger(__name__)

__all__ = [
    "APPLICABILITIES",
    "ATTENTION_STATUSES",
    "CONSUMERS",
    "STATUS_MISSING",
    "STATUS_DEGRADED",
    "STATUS_UNCHECKED",
    "STATUS_UNRESOLVABLE",
    "STATUS_VERIFIED",
    "HardwareProfile",
    "HardwareProfileError",
    "ProfileResolution",
    "create_profile_version",
    "get_profile",
    "in_use_report",
    "list_names",
    "list_versions",
    "record_resolution",
    "require_applicability",
    "resolve_for",
    "resolve_profile",
    "to_ray_actor_options",
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


#: Spec §2: a profile name is a slug. It becomes a CLI argument, a URL path segment on the
#: dashboard and an MLflow tag value (``name@vN``), so nothing outside this set is accepted.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _validate_shape(
    name: str,
    gpu_count: int,
    cpu: float,
    memory_gb: float,
    nodes: int,
    mig_profile: str | None,
) -> None:
    """Refuse a shape no consumer could honour (spec §2 field constraints)."""
    if not _NAME_RE.match(name or ""):
        raise HardwareProfileError(
            f"profile name {name!r} must be a slug: lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit, at most 63 characters"
        )
    if gpu_count < 0:
        raise HardwareProfileError(f"gpu_count must be >= 0, got {gpu_count!r}")
    if cpu < 0:
        raise HardwareProfileError(f"cpu must be >= 0, got {cpu!r}")
    if memory_gb < 0:
        raise HardwareProfileError(f"memory_gb must be >= 0, got {memory_gb!r}")
    if nodes < 1:
        raise HardwareProfileError(f"nodes must be >= 1, got {nodes!r}")
    if mig_profile is not None:
        from examlops.gpu_sharing import MIG_FRACTIONS  # noqa: PLC0415 - ADR 0030 vocabulary

        if mig_profile not in MIG_FRACTIONS:
            raise HardwareProfileError(
                f"mig_profile must be one of {sorted(MIG_FRACTIONS)}, got {mig_profile!r}"
            )


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
    _validate_shape(name, gpu_count, cpu, memory_gb, nodes, mig_profile)
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
    """Adapt a resolution into the placement seam's ask (used by ``--cluster auto``).

    Carries the profile's ``gpu_fraction``/``mig_profile`` (ADR 0030 decision 1), re-read from
    the exact version resolved, so a fractional profile is placed — and later mapped onto the
    scheduler — as the fraction it declares rather than as whole GPUs.
    """
    r = resolution.resources
    profile = get_profile(resolution.name, version=resolution.version)
    fraction = profile.gpu_fraction if profile is not None else 1.0
    mig = profile.mig_profile if profile is not None else None
    return hpc_placement.ResourceAsk(
        gpus=r.gpus, cpus=r.cpus, nodes=r.nodes, gpu_fraction=fraction, mig_profile=mig
    )


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


def to_ray_actor_options(resolution: ProfileResolution) -> dict[str, float]:
    """Adapt a resolution into Ray Serve ``ray_actor_options`` (spec §4 Phase 3, serving).

    Mirrors :func:`examlops.autoscale.to_ray_deployment_kwargs`, which already maps a policy's
    ``gpu_fraction`` onto ``ray_actor_options={"num_gpus": ...}`` — a profile is sugar over that
    same seam, never a second way to describe a replica's share of a node.

    ``num_gpus`` is ``gpu_count × gpu_fraction`` (so one whole GPU at ``fraction=0.5`` asks for
    ``0.5``, exactly as the autoscale policy does); a key is emitted only when the profile
    actually asks for that dimension, so a CPU-only profile never pins ``num_gpus=0`` over Ray's
    own default. Like :func:`to_workload`, it re-reads the exact version it resolved rather than
    whichever version the ``active`` label points at now.
    """
    profile = get_profile(resolution.name, version=resolution.version)
    if profile is None:
        raise HardwareProfileError(
            f"hardware profile {resolution.name!r} version {resolution.version} no longer exists"
        )
    options: dict[str, float] = {}
    num_gpus = profile.gpu_count * profile.gpu_fraction
    if num_gpus > 0:
        options["num_gpus"] = round(num_gpus, 6)
    if profile.cpu > 0:
        options["num_cpus"] = profile.cpu
    return options


def require_applicability(profile: HardwareProfile, need: str) -> None:
    """Refuse a profile whose ``applicability`` does not cover *need* (spec §4, Phase 2/3).

    A silent ignore is the failure mode this exists to prevent: a serving profile quietly
    accepted for a training run would place a job against a shape nobody declared it for.
    The error names the profile's **actual** applicability so the operator can see why.
    """
    if need not in APPLICABILITIES:
        raise HardwareProfileError(
            f"unknown applicability {need!r}, expected one of {list(APPLICABILITIES)}"
        )
    if need in profile.applicability or "any" in profile.applicability:
        return
    raise HardwareProfileError(
        f"hardware profile {profile.name!r} (version {profile.version}) is not applicable to "
        f"{need!r}: its applicability is {list(profile.applicability)}. Create a version that "
        f"includes {need!r} or 'any' — "
        f"exa hardware profile set {profile.name} --applicability {need}"
    )


def resolve_for(
    name: str,
    need: str,
    *,
    label: str = "active",
    version: int | None = None,
    target_cluster: str | None = None,
    consumer_ref: str | None = None,
    project: str | None = None,
    actor: str | None = None,
) -> tuple[HardwareProfile, ProfileResolution]:
    """:func:`resolve_profile` with the Phase 2/3 applicability gate in front of it.

    Returns the exact version that was checked together with its resolution — the resolution is
    taken at that pinned version, so a concurrent ``exa hardware profile set`` cannot move the
    ``active`` label between the check and the resolve.

    ``consumer_ref`` (Phase 4) names the thing that is being sized — a workbench
    (``project/name``), a training model or run id, a served model. When given, the resolution is
    appended to the ``hardware_profile_resolutions`` ledger with ``need`` as the consumer, so the
    status it got stays visible (``exa hardware profile in-use``, ``exa status``) after the
    command that resolved it has exited.
    """
    profile = get_profile(name, label=label, version=version)
    if profile is None:
        ref = f"version {version}" if version is not None else f"label {label!r}"
        raise HardwareProfileError(f"hardware profile {name!r} ({ref}) not found")
    require_applicability(profile, need)
    resolution = resolve_profile(name, version=profile.version, target_cluster=target_cluster)
    if consumer_ref:
        record_resolution(
            resolution,
            consumer=need,
            consumer_ref=consumer_ref,
            target_cluster=target_cluster,
            project=project,
            actor=actor,
        )
    return profile, resolution


def record_resolution(
    resolution: ProfileResolution,
    *,
    consumer: str,
    consumer_ref: str,
    target_cluster: str | None = None,
    project: str | None = None,
    actor: str | None = None,
) -> bool:
    """Append ``resolution`` to the ledger for ``consumer``/``consumer_ref`` (ADR 0157 Phase 4).

    Fail-open by design: the ledger is the *visibility* surface, not a gate — the gate is
    :func:`require_applicability` and the ``unresolvable`` refusal, which already ran. A ledger
    write that fails (read-only datastore, locked file) is logged as a warning and reported by the
    ``False`` return, never raised into a workbench create or a training launch that is otherwise
    valid. Returns ``True`` when the row was written. An unknown ``consumer`` or an empty
    ``consumer_ref`` is a programming error and raises.
    """
    if consumer not in CONSUMERS:
        raise HardwareProfileError(f"unknown consumer {consumer!r}, expected one of {CONSUMERS}")
    if not consumer_ref or not consumer_ref.strip():
        raise HardwareProfileError("consumer_ref must not be empty")
    try:
        _data.record_resolution(
            resolution.name,
            resolution.version,
            consumer=consumer,
            consumer_ref=consumer_ref.strip(),
            status=resolution.status,
            reason=resolution.reason,
            unconfirmed=resolution.unconfirmed,
            target_cluster=target_cluster,
            project=project,
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001 - visibility must never break the call it observes
        logger.warning(
            "hardware profile %s v%s: could not record the %s resolution for %s (%s)",
            resolution.name,
            resolution.version,
            consumer,
            consumer_ref,
            exc,
        )
        return False
    logger.info(
        "hardware_profile_resolved name=%s version=%s consumer=%s ref=%s status=%s",
        resolution.name,
        resolution.version,
        consumer,
        consumer_ref,
        resolution.status,
    )
    return True


def _entry(
    *,
    consumer: str,
    consumer_ref: str,
    project: str | None,
    name: str,
    version: int,
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    """One in-use line: the latest ledger status, overridden by ``missing`` when the version is gone."""
    exists = get_profile(name, version=version) is not None
    if not exists:
        status = STATUS_MISSING
        reason = f"version {version} of {name!r} was deleted; this {consumer} still references it"
    elif row is not None:
        status, reason = row["status"], row.get("reason") or ""
    else:
        # Created before the ledger existed (or its write failed): nothing was ever checked.
        status, reason = STATUS_UNCHECKED, "no recorded resolution"
    return {
        "consumer": consumer,
        "consumer_ref": consumer_ref,
        "project": project,
        "name": name,
        "version": version,
        "status": status,
        "reason": reason,
        "unconfirmed": list((row or {}).get("unconfirmed") or []),
        "target_cluster": (row or {}).get("target_cluster"),
        "resolved_at": (row or {}).get("ts"),
        "exists": exists,
    }


def in_use_report(
    *,
    days: float = 7.0,
    project: str | None = None,
    projects: Iterable[str | None] | None = None,
) -> dict[str, Any]:
    """Every profile currently in use, with the honest status it resolved to (ADR 0157 Phase 4).

    "In use" is (a) every RUNNING workbench created from a profile — whatever its age — and
    (b) every training/serving consumer whose latest ledger resolution is within ``days``. Each
    entry carries the latest recorded status for that consumer; a version that has since been
    deleted reports ``missing``. ``attention`` lists the ``degraded``/``unresolvable``/``missing``
    entries — the ones ADR 0157's Consequences say must never hide behind a catalog that merely
    stores intent. ``project`` scopes both sources to one project; ``projects`` is the tenant
    scope (the set a caller may read; ``None`` in it admits rows recorded with no project) —
    both are applied in SQL before any limit. ``truncated`` is ``True`` when more
    training/serving consumers matched than one bounded read returns, so a partial report never
    reads as a complete one.
    """
    if days <= 0:
        raise HardwareProfileError(f"days must be > 0, got {days!r}")
    from examlops.workbenches import list_workbenches  # noqa: PLC0415 - avoids an import cycle

    scope = set(projects) if projects is not None else None
    entries: list[dict[str, Any]] = []
    for wb in list_workbenches(project=project):
        if (wb.get("status") or "").upper() != "RUNNING" or not wb.get("hardware_profile"):
            continue
        if scope is not None and wb.get("project") not in scope:
            continue
        version = wb.get("hardware_profile_version")
        if version is None:
            continue
        ref = f"{wb['project']}/{wb['name']}"
        rows = _data.list_resolutions(
            wb["hardware_profile"], consumer="workbench", consumer_ref=ref, limit=1
        )
        entries.append(
            _entry(
                consumer="workbench",
                consumer_ref=ref,
                project=wb["project"],
                name=wb["hardware_profile"],
                version=int(version),
                row=rows[0] if rows else None,
            )
        )

    truncated = False
    for consumer in ("training", "serving"):
        # Latest row per consumer_ref, reduced in SQL: a raw newest-N page de-duplicated here
        # silently dropped every consumer whose latest row fell behind a busier one's rows.
        rows, more = _data.list_latest_resolutions(
            consumer, project=project, projects=scope, since_days=days
        )
        truncated = truncated or more
        for row in rows:
            entries.append(
                _entry(
                    consumer=consumer,
                    consumer_ref=row["consumer_ref"],
                    project=row.get("project"),
                    name=row["name"],
                    version=int(row["version"]),
                    row=row,
                )
            )

    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {
        "window_days": days,
        "project": project,
        "entries": entries,
        "counts": counts,
        "attention": [e for e in entries if e["status"] in ATTENTION_STATUSES],
        "truncated": truncated,
    }

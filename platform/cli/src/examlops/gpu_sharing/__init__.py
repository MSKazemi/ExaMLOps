"""Next-Gen 40 · E3 — GPU sharing & fractional allocation (ADR 0030).

First-class fractional GPU allocation across the scheduler abstraction and the K8s serving
path: a request asks for a *fraction* of a GPU (or a MIG profile); the platform picks the
best available mechanism given cluster capabilities, bin-packs fractional requests onto
whole GPUs, surfaces the isolation level honestly, and accounts fractional GPU-hours.

Mechanisms (best → weakest isolation):
- **mig** — NVIDIA Multi-Instance GPU: hardware-partitioned, strong isolation.
- **timeslice** — time-sliced sharing: soft isolation (no memory/SM partition).
- **whole** — a full GPU (fallback / fraction ≥ 1).

Honest fallback (R): if a sub-1.0 fraction is requested but the cluster supports no
fractional mechanism, the request is rounded up to a **whole** GPU and the wasted capacity
is surfaced — never silently pretended to be fractional.

Pure-Python planning/accounting — no GPU required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# MIG profile → GPU fraction (A100 7-slice model, illustrative).
MIG_FRACTIONS = {
    "1g.5gb": 1 / 7,
    "2g.10gb": 2 / 7,
    "3g.20gb": 3 / 7,
    "4g.20gb": 4 / 7,
    "7g.40gb": 1.0,
}

_ISOLATION = {"mig": "hardware", "timeslice": "soft", "whole": "exclusive"}


@dataclass
class FractionalAsk:
    """A request for a fraction of a GPU (or a named MIG profile)."""

    label: str
    fraction: float = 1.0
    mig_profile: str | None = None

    @property
    def effective_fraction(self) -> float:
        if self.mig_profile and self.mig_profile in MIG_FRACTIONS:
            return MIG_FRACTIONS[self.mig_profile]
        return self.fraction


@dataclass
class ClusterGpuCaps:
    """What fractional mechanisms a cluster/node supports.

    The scheduler-specific fields are only read by :mod:`examlops.gpu_sharing.scheduler_map`
    (ADR 0030 decision 3) and default to "not configured", so a cluster that declares nothing
    keeps the honest whole-GPU fallback:

    * ``shards_per_gpu`` — Slurm ``gres/shard`` count per physical GPU (Slurm's GPU sharing);
      ``0`` means Slurm cannot express time-sliced sharing on this cluster.
    * ``mig_gres_types`` — MIG profile → Slurm GRES *type* name (``gres.conf`` names them per
      site, e.g. ``1g.5gb`` → ``a100_1g.5gb``); a profile absent here is requested by its own name.
    * ``flux_mig_properties`` — MIG profile → Flux node property whose resource set (R) exposes
      that slice as a GPU; Flux has no GPU-fraction flag, so without a property MIG is unmapped.
    """

    supports_mig: bool = False
    supports_timeslice: bool = False
    mig_profiles: list[str] = field(default_factory=list)
    shards_per_gpu: int = 0
    mig_gres_types: dict[str, str] = field(default_factory=dict)
    flux_mig_properties: dict[str, str] = field(default_factory=dict)


@dataclass
class MechanismChoice:
    mechanism: str  # mig | timeslice | whole
    isolation: str  # hardware | soft | exclusive
    allocated_fraction: float
    requested_fraction: float
    wasted_fraction: float  # capacity paid for but unused (honest fallback)
    note: str
    mig_profile: str | None = None  # the MIG profile chosen (only for mechanism == "mig")

    def as_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "isolation": self.isolation,
            "allocated_fraction": self.allocated_fraction,
            "requested_fraction": self.requested_fraction,
            "wasted_fraction": self.wasted_fraction,
            "note": self.note,
            "mig_profile": self.mig_profile,
        }


def select_mechanism(ask: FractionalAsk, caps: ClusterGpuCaps) -> MechanismChoice:
    """Capability-aware mechanism selection with honest fallback (R)."""
    frac = ask.effective_fraction
    if frac >= 1.0:
        return MechanismChoice("whole", "exclusive", 1.0, frac, 0.0, "full GPU")

    if ask.mig_profile and caps.supports_mig and ask.mig_profile in caps.mig_profiles:
        return MechanismChoice(
            "mig",
            "hardware",
            frac,
            frac,
            0.0,
            f"MIG {ask.mig_profile} (hardware isolation)",
            mig_profile=ask.mig_profile,
        )
    if caps.supports_mig and caps.mig_profiles:
        # Snap the fraction up to the smallest MIG profile that fits.
        for profile in sorted(caps.mig_profiles, key=lambda p: MIG_FRACTIONS.get(p, 1.0)):
            if MIG_FRACTIONS.get(profile, 1.0) >= frac:
                alloc = MIG_FRACTIONS[profile]
                return MechanismChoice(
                    "mig",
                    "hardware",
                    alloc,
                    frac,
                    alloc - frac,
                    f"MIG {profile} snapped up from {frac:.2f}",
                    mig_profile=profile,
                )
    if caps.supports_timeslice:
        return MechanismChoice(
            "timeslice",
            "soft",
            frac,
            frac,
            0.0,
            "time-sliced (SOFT isolation — no memory/SM partition)",
        )
    # Honest fallback: no fractional support → whole GPU, surface the waste.
    return MechanismChoice(
        "whole",
        "exclusive",
        1.0,
        frac,
        1.0 - frac,
        f"no fractional support — rounded {frac:.2f} up to a whole GPU "
        f"({(1.0 - frac) * 100:.0f}% wasted)",
    )


@dataclass
class Placement:
    ask_label: str
    gpu_index: int
    fraction: float
    mechanism: str
    isolation: str


@dataclass
class PackResult:
    placements: list[Placement] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    gpus_used: int = 0


def bin_pack(asks: list[FractionalAsk], gpu_count: int, caps: ClusterGpuCaps) -> PackResult:
    """First-fit-decreasing bin-packing of fractional asks onto whole GPUs (R)."""
    result = PackResult()
    remaining = [1.0] * gpu_count  # free capacity per GPU
    mech_by_gpu: list[str | None] = [None] * gpu_count
    # Largest first for better packing.
    ordered = sorted(asks, key=lambda a: select_mechanism(a, caps).allocated_fraction, reverse=True)
    for ask in ordered:
        choice = select_mechanism(ask, caps)
        need = choice.allocated_fraction
        placed = False
        for i in range(gpu_count):
            # Don't mix mechanisms on one GPU (timeslice vs mig vs whole).
            if remaining[i] + 1e-9 >= need and (
                mech_by_gpu[i] is None or mech_by_gpu[i] == choice.mechanism
            ):
                remaining[i] -= need
                mech_by_gpu[i] = choice.mechanism
                result.placements.append(
                    Placement(ask.label, i, need, choice.mechanism, choice.isolation)
                )
                placed = True
                break
        if not placed:
            result.unplaced.append(ask.label)
    result.gpus_used = sum(1 for r in remaining if r < 1.0 - 1e-9)
    return result


def fractional_gpu_hours(fraction: float, seconds: float) -> float:
    """Fractional GPU-hour accounting (R): fraction × wall-hours."""
    return fraction * (seconds / 3600.0)


def isolation_level(mechanism: str) -> str:
    return _ISOLATION.get(mechanism, "unknown")


def record_allocation(
    model: str,
    choice: MechanismChoice,
    *,
    tenant: str = "default",
    gpu_index: int | None = None,
    job_id: str | None = None,
    scheduler: str | None = None,
    actor: str | None = None,
) -> None:
    """Persist a GPU allocation for fractional accounting, audited in the same transaction.

    ``job_id``/``scheduler`` link the allocation to the scheduler job it was submitted as, which
    is what lets ``exa models cost --record`` bill that job's GPU-hours at the allocated fraction
    (ADR 0030 decision 5). A planning-only record (``exa hpc gpu-share plan --record``) has no job
    and is never used for billing — accounting never guesses which allocation a job ran under.
    """
    import os

    from examlops import data as platform_db
    from examlops.data.audit import append_audit_event

    platform_db.init_db()
    who = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    with platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO gpu_allocations (model, tenant, mechanism, fraction, isolation, "
            "gpu_index, note, job_id, scheduler, requested_fraction) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                model,
                tenant,
                choice.mechanism,
                choice.allocated_fraction,
                choice.isolation,
                gpu_index,
                choice.note,
                None if job_id is None else str(job_id),
                scheduler.strip().lower() if scheduler else None,
                choice.requested_fraction,
            ),
        )
        append_audit_event(
            conn,
            "gpu-sharing",
            who,
            "gpu_allocation_recorded",
            model,
            {
                "mechanism": choice.mechanism,
                "allocated_fraction": choice.allocated_fraction,
                "requested_fraction": choice.requested_fraction,
                "wasted_fraction": choice.wasted_fraction,
                "job_id": job_id,
                "scheduler": scheduler,
            },
            tenant=tenant,
        )


def list_allocations(*, tenant: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Newest allocations first; the tenant filter is applied in SQL, before the LIMIT."""
    from examlops import data as platform_db

    platform_db.init_db()
    limit = max(1, min(int(limit), 1000))
    where = "WHERE tenant=?" if tenant else ""
    params: tuple[Any, ...] = ((tenant,) if tenant else ()) + (limit,)
    with platform_db.get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM gpu_allocations {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def allocation_for_job(job_id: str, *, scheduler: str | None = None) -> dict[str, Any] | None:
    """The allocation a scheduler job was submitted under (newest wins), else ``None``.

    Job ids are only unique *within* a scheduler: Slurm job ``4711`` and a Flux or mock job that
    happens to share the id are different jobs. A billing caller therefore passes ``scheduler``,
    and only a row recorded for that scheduler matches — a job is never billed at the fraction of
    another scheduler's job.
    """
    if not job_id:
        return None
    from examlops import data as platform_db

    platform_db.init_db()
    sql = "SELECT * FROM gpu_allocations WHERE job_id=?"
    params: tuple[Any, ...] = (str(job_id),)
    if scheduler is not None:
        sql += " AND scheduler=?"
        params += (scheduler.strip().lower(),)
    with platform_db.get_db() as conn:
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return dict(row) if row else None


def caps_from_capabilities(capabilities: dict[str, Any] | None) -> ClusterGpuCaps:
    """Read a registered cluster's declared ``capabilities`` into :class:`ClusterGpuCaps`.

    Tolerant of absent/malformed keys (they read as "not supported") — a cluster that declares
    nothing about GPU sharing gets the honest whole-GPU fallback, never an assumed mechanism.
    """
    caps = capabilities if isinstance(capabilities, dict) else {}
    profiles = caps.get("mig_profiles")
    mig_profiles = [str(p) for p in profiles] if isinstance(profiles, list) else []

    def _str_map(key: str) -> dict[str, str]:
        raw = caps.get(key)
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}

    shards = caps.get("shards_per_gpu")
    try:
        shards_per_gpu = int(shards) if shards is not None and not isinstance(shards, bool) else 0
    except (TypeError, ValueError):
        shards_per_gpu = 0
    return ClusterGpuCaps(
        supports_mig=bool(caps.get("supports_mig")) or bool(mig_profiles),
        supports_timeslice=bool(caps.get("supports_timeslice")) or shards_per_gpu > 0,
        mig_profiles=mig_profiles,
        shards_per_gpu=max(0, shards_per_gpu),
        mig_gres_types=_str_map("mig_gres_types"),
        flux_mig_properties=_str_map("flux_mig_properties"),
    )


#: Keys of a cluster's ``capabilities`` that describe GPU sharing (exported to a run's env).
SHARING_CAPABILITY_KEYS = (
    "supports_mig",
    "supports_timeslice",
    "mig_profiles",
    "shards_per_gpu",
    "mig_gres_types",
    "flux_mig_properties",
)


__all__ = [
    "MIG_FRACTIONS",
    "FractionalAsk",
    "ClusterGpuCaps",
    "MechanismChoice",
    "Placement",
    "PackResult",
    "select_mechanism",
    "bin_pack",
    "fractional_gpu_hours",
    "isolation_level",
    "record_allocation",
    "list_allocations",
    "allocation_for_job",
    "caps_from_capabilities",
    "SHARING_CAPABILITY_KEYS",
]

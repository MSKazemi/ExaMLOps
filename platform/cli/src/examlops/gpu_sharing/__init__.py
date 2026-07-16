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
    """What fractional mechanisms a cluster/node supports."""

    supports_mig: bool = False
    supports_timeslice: bool = False
    mig_profiles: list[str] = field(default_factory=list)


@dataclass
class MechanismChoice:
    mechanism: str  # mig | timeslice | whole
    isolation: str  # hardware | soft | exclusive
    allocated_fraction: float
    requested_fraction: float
    wasted_fraction: float  # capacity paid for but unused (honest fallback)
    note: str


def select_mechanism(ask: FractionalAsk, caps: ClusterGpuCaps) -> MechanismChoice:
    """Capability-aware mechanism selection with honest fallback (R)."""
    frac = ask.effective_fraction
    if frac >= 1.0:
        return MechanismChoice("whole", "exclusive", 1.0, frac, 0.0, "full GPU")

    if ask.mig_profile and caps.supports_mig and ask.mig_profile in caps.mig_profiles:
        return MechanismChoice(
            "mig", "hardware", frac, frac, 0.0, f"MIG {ask.mig_profile} (hardware isolation)"
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
) -> None:
    """Persist a GPU allocation for fractional accounting."""
    from examlops import platform_db

    platform_db.init_db()
    with platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO gpu_allocations (model, tenant, mechanism, fraction, isolation, "
            "gpu_index, note) VALUES (?,?,?,?,?,?,?)",
            (
                model,
                tenant,
                choice.mechanism,
                choice.allocated_fraction,
                choice.isolation,
                gpu_index,
                choice.note,
            ),
        )


def list_allocations(*, tenant: str | None = None) -> list[dict[str, Any]]:
    from examlops import platform_db

    platform_db.init_db()
    where = "WHERE tenant=?" if tenant else ""
    params = (tenant,) if tenant else ()
    with platform_db.get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM gpu_allocations {where} ORDER BY id DESC LIMIT 200", params
        ).fetchall()
    return [dict(r) for r in rows]


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
]

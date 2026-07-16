"""Next-Gen 40 · E8 — heterogeneous hardware & hybrid HPC↔cloud (ADR 0041).

ExaMLOps implicitly assumed NVIDIA/CUDA on one HPC cluster. European HPC is increasingly
heterogeneous (AMD MI300, Intel Gaudi, TPU, CPU) and bursting to cloud is common when on-prem
is saturated. E8 lets a workload declare its device requirements **neutrally** — an
``accelerator`` + capability tags + an optional ``target`` (hpc|cloud) — and places it on the
**best-available compatible** device across HPC and cloud pools, with:

- a **portability check** — the engine/artifact must actually run on the target device; an
  incompatible placement is **rejected with a clear error**, never scheduled silently (R4/GWT-2);
- **honest fallback** — when the requested accelerator is unavailable, fall back to another
  compatible device and say so; a fractional request on a vendor without fractions gets the whole
  device, flagged (R3/GWT-3);
- **governed cloud bursting** — opt-in only, blocked + audited when data-residency forbids
  egress (R5/GWT-4); data movement is explicit, never silent;
- **per-device accounting** — device type + region flow into cost + carbon (R6/GWT-5).

The default single-cluster NVIDIA path is unchanged: everything here is additive and degrades
to CPU + mocked device pools with no vendor SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from examlops import platform_db

ACCELERATORS = ("nvidia", "amd", "intel-gaudi", "tpu", "cpu")

# Neutral accelerator → the engine backend it needs.
_BACKEND = {
    "nvidia": "cuda",
    "amd": "rocm",
    "intel-gaudi": "ipex",
    "tpu": "xla",
    "cpu": "cpu",
}

# Engines (E2) declare which backends they support. ``generic`` = framework-native/portable.
ENGINE_BACKENDS: dict[str, tuple[str, ...]] = {
    "vllm": ("cuda", "rocm"),
    "sglang": ("cuda",),
    "ipex": ("ipex", "cpu"),
    "cpu": ("cpu",),
    "generic": ("cuda", "rocm", "ipex", "xla", "cpu"),
}

# Which vendors expose a real GPU-fractioning mechanism (NVIDIA MIG). Others fall back honestly.
_SUPPORTS_FRACTIONS = {"nvidia": True}

# Residency classes that forbid leaving the on-prem estate.
_NO_EGRESS = {"no-egress", "restricted"}
_EU_REGIONS = {"eu", "eu-central", "eu-west", "eu-north", "eu-south"}


class PortabilityError(RuntimeError):
    """Raised when an engine/artifact cannot run on the requested device (R4/GWT-2)."""


@dataclass
class Workload:
    name: str
    accelerator: str = "nvidia"
    capabilities: list[str] = field(default_factory=list)
    target: str | None = None  # hpc|cloud|None(=any)
    engine: str = "generic"
    fraction: float = 1.0
    residency: str = "open"  # open|eu-only|no-egress
    allow_burst: bool = False


@dataclass
class Placement:
    workload: str
    pool: str
    accelerator: str
    target: str
    region: str | None
    cost_per_hour: float
    carbon_factor: float
    fallback: bool = False
    fraction_honored: bool = True
    note: str = ""


@dataclass
class Rejection:
    workload: str
    reason: str


def portable(engine: str, accelerator: str) -> bool:
    """True if ``engine`` supports the backend that ``accelerator`` requires (R2)."""
    backend = _BACKEND.get(accelerator)
    if backend is None:
        return False
    return backend in ENGINE_BACKENDS.get(engine, ())


def supports_fractions(accelerator: str) -> bool:
    return _SUPPORTS_FRACTIONS.get(accelerator, False)


def _eligible(w: Workload, pool: dict, accelerator: str) -> bool:
    if pool["accelerator"] != accelerator:
        return False
    if w.target and pool["target"] != w.target:
        return False
    if pool["count"] <= 0:
        return False
    return set(w.capabilities) <= set(pool.get("capabilities", []))


def place(
    workload: Workload,
    pools: list[dict] | None = None,
    *,
    actor: str | None = None,
    record: bool = True,
) -> Placement | Rejection:
    """Place a workload on the best-available compatible device (R2/R3/R4).

    Order: (1) portability-gate the engine against the requested accelerator — reject if it
    cannot run there; (2) prefer the requested accelerator (cheapest eligible pool); (3) else
    honestly fall back to any other accelerator the engine supports. A sub-1.0 fraction on a
    vendor without fractioning is honored as a whole device and flagged.
    """
    if workload.accelerator not in ACCELERATORS:
        raise ValueError(f"accelerator must be one of {ACCELERATORS}, got {workload.accelerator!r}")

    # R4/GWT-2 — never schedule an engine that cannot run on the requested device.
    if not portable(workload.engine, workload.accelerator):
        rej = Rejection(
            workload.name,
            f"engine {workload.engine!r} cannot run on {workload.accelerator} "
            f"(needs backend {_BACKEND[workload.accelerator]}; supports "
            f"{ENGINE_BACKENDS.get(workload.engine, ())})",
        )
        if record:
            _record_reject(workload, rej.reason)
        return rej

    if pools is None:
        pools = platform_db.get_device_pools(status="active")

    # (2) requested accelerator, cheapest first (pools already cost-ordered by the query).
    preferred = [p for p in pools if _eligible(workload, p, workload.accelerator)]
    chosen, fallback = (preferred[0], False) if preferred else (None, False)

    # (3) honest fallback to any engine-compatible accelerator.
    if chosen is None:
        alts = [
            p
            for p in pools
            if p["accelerator"] != workload.accelerator
            and portable(workload.engine, p["accelerator"])
            and _eligible(workload, p, p["accelerator"])
        ]
        alts.sort(key=lambda p: p["cost_per_hour"])
        if alts:
            chosen, fallback = alts[0], True

    if chosen is None:
        rej = Rejection(workload.name, "no compatible device pool available")
        if record:
            _record_reject(workload, rej.reason)
        return rej

    frac_ok = True
    note = ""
    if workload.fraction < 1.0 and not supports_fractions(chosen["accelerator"]):
        frac_ok = False
        note = f"{chosen['accelerator']} has no GPU fractioning — whole device allocated"
    if fallback:
        note = (note + "; " if note else "") + (
            f"requested {workload.accelerator} unavailable — fell back to {chosen['accelerator']}"
        )

    placement = Placement(
        workload=workload.name,
        pool=chosen["name"],
        accelerator=chosen["accelerator"],
        target=chosen["target"],
        region=chosen.get("region"),
        cost_per_hour=chosen["cost_per_hour"],
        carbon_factor=chosen["carbon_factor"],
        fallback=fallback,
        fraction_honored=frac_ok,
        note=note,
    )
    if record:
        platform_db.record_placement_decision(
            workload.name,
            accelerator_requested=workload.accelerator,
            device_chosen=chosen["accelerator"],
            pool=chosen["name"],
            target=chosen["target"],
            region=chosen.get("region"),
            decision="fallback" if fallback else "placed",
            fraction_honored=frac_ok,
            reason=note or None,
        )
        _audit(
            workload.name, "hardware_place", {"pool": chosen["name"], "fallback": fallback}, actor
        )
    return placement


def can_burst(workload: Workload, cloud_region: str | None) -> tuple[bool, str]:
    """Decide whether a workload may burst to a cloud region under its residency (R5/GWT-4)."""
    if not workload.allow_burst:
        return False, "cloud burst is opt-in and was not enabled for this workload"
    if workload.residency in _NO_EGRESS:
        return False, f"data-residency {workload.residency!r} forbids egress to cloud"
    if workload.residency == "eu-only" and (cloud_region or "").lower() not in _EU_REGIONS:
        return False, f"data-residency eu-only forbids egress to region {cloud_region!r}"
    return True, "burst permitted"


def plan_burst(
    workload: Workload,
    cloud_pools: list[dict] | None = None,
    *,
    from_pool: str = "hpc",
    actor: str | None = None,
) -> Placement | Rejection:
    """Attempt a governed HPC→cloud burst; blocked + audited when residency forbids egress."""
    if cloud_pools is None:
        cloud_pools = platform_db.get_device_pools(target="cloud", status="active")

    # Pick the first eligible cloud pool to name a concrete region in the audit trail.
    candidates = [p for p in cloud_pools if portable(workload.engine, p["accelerator"])]
    region = candidates[0].get("region") if candidates else None

    allowed, reason = can_burst(workload, region)
    platform_db.record_burst_event(
        workload.name,
        from_pool=from_pool,
        to_pool=candidates[0]["name"] if (allowed and candidates) else None,
        residency=workload.residency,
        allowed=allowed,
        reason=reason,
    )
    _audit(workload.name, "hardware_burst", {"allowed": allowed, "reason": reason}, actor)
    if not allowed:
        return Rejection(workload.name, reason)
    if not candidates:
        return Rejection(workload.name, "no compatible cloud pool available for burst")
    cloud_workload = Workload(
        workload.name,
        accelerator=candidates[0]["accelerator"],
        capabilities=workload.capabilities,
        target="cloud",
        engine=workload.engine,
        fraction=workload.fraction,
        residency=workload.residency,
        allow_burst=True,
    )
    return place(cloud_workload, candidates, actor=actor)


def device_accounting(placement: Placement, hours: float) -> dict:
    """Per-device cost + carbon for a placed workload (R6/GWT-5)."""
    return {
        "device": placement.accelerator,
        "region": placement.region,
        "hours": hours,
        "cost": round(placement.cost_per_hour * hours, 4),
        "carbon_g": round(placement.carbon_factor * hours, 4),
    }


def _record_reject(workload: Workload, reason: str) -> None:
    platform_db.record_placement_decision(
        workload.name,
        accelerator_requested=workload.accelerator,
        device_chosen=None,
        pool=None,
        target=workload.target,
        region=None,
        decision="rejected",
        reason=reason,
    )


def _audit(workload: str, action: str, extra: dict, actor: str | None) -> None:
    try:
        platform_db.write_audit_event("exa-hardware", actor, action, workload, extra)
    except Exception:
        pass


__all__ = [
    "ACCELERATORS",
    "ENGINE_BACKENDS",
    "PortabilityError",
    "Workload",
    "Placement",
    "Rejection",
    "portable",
    "supports_fractions",
    "place",
    "can_burst",
    "plan_burst",
    "device_accounting",
]

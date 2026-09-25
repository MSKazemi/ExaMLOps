"""ADR 0030 decision 3 — map a fractional GPU ask onto what Slurm / Flux can actually express.

The scheduler adapters take a scheduler-neutral ``resources`` dict (``gpus``, ``nodes``, …). A
fraction of a GPU is not something either scheduler understands natively, so this module turns
``(ask, cluster capabilities)`` into the concrete keys each adapter emits, **only where the cluster
declares the mechanism**, and otherwise falls back to a whole GPU with an explicit warning — never
a silent over-commit (a fraction the scheduler cannot enforce passed off as one it did):

=========  ================================================  ======================================
Scheduler  MIG (hardware isolation)                          Time-slice (soft isolation)
=========  ================================================  ======================================
slurm      ``--gres=gpu:<type>:<n>`` — the MIG slice's GRES  ``--gres=shard:<k>`` — Slurm's GPU
           type (``mig_gres_types`` maps profile → site's    sharding; ``k = ceil(n·f·S)`` where
           ``gres.conf`` name; default the profile itself)   ``S = shards_per_gpu`` declared
flux       ``-g<n> --requires=<property>`` when the cluster  not expressible (flux-core ``-g`` takes
           declares a property whose R exposes that slice    whole GPUs) → whole-GPU fallback
           as a GPU (``flux_mig_properties``)
mock       unchanged resources; the planned choice is still  same
           returned so it can be recorded
=========  ================================================  ======================================

Pure: no scheduler is contacted. The adapters stay unchanged apart from Slurm learning ``gres``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

from examlops.gpu_sharing import (
    MIG_FRACTIONS,
    SHARING_CAPABILITY_KEYS,
    ClusterGpuCaps,
    FractionalAsk,
    MechanismChoice,
    caps_from_capabilities,
    select_mechanism,
)

#: Env a run subprocess reads (set by ``exa pipeline run --hardware-profile`` / ``--cluster``).
ENV_FRACTION = "EXAMLOPS_HPC_GPU_FRACTION"
ENV_MIG_PROFILE = "EXAMLOPS_HPC_MIG_PROFILE"
ENV_SHARING_CAPS = "EXAMLOPS_HPC_GPU_SHARING"

SCHEDULERS = ("slurm", "flux", "mock")


class GpuSharingError(ValueError):
    """A fractional ask that is malformed (never raised for an unsupported mechanism)."""


@dataclass
class SchedulerGpuMapping:
    """The adapter-ready resources plus what was actually allocated and why."""

    scheduler: str
    resources: dict[str, Any]
    choice: MechanismChoice
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scheduler": self.scheduler,
            "resources": self.resources,
            "choice": self.choice.as_dict(),
            "warnings": self.warnings,
        }


def _whole(ask: FractionalAsk, reason: str) -> MechanismChoice:
    frac = ask.effective_fraction
    return MechanismChoice(
        "whole",
        "exclusive",
        1.0,
        frac,
        max(0.0, 1.0 - frac),
        f"{reason} — rounded {frac:.2f} up to a whole GPU ({(1.0 - frac) * 100:.0f}% wasted)",
    )


def _validate(ask: FractionalAsk, gpus: int) -> None:
    if gpus < 1:
        raise GpuSharingError(f"a GPU-sharing ask needs at least one GPU, got gpus={gpus}")
    if ask.mig_profile is not None and ask.mig_profile not in MIG_FRACTIONS:
        raise GpuSharingError(
            f"unknown MIG profile {ask.mig_profile!r} (known: {', '.join(MIG_FRACTIONS)})"
        )
    if not (0.0 < ask.fraction <= 1.0):
        raise GpuSharingError(f"gpu fraction must be in (0, 1], got {ask.fraction!r}")


def _nodes(resources: dict[str, Any]) -> int:
    raw = resources.get("nodes")
    if raw is None or raw == "":
        return 1
    try:
        nodes = int(str(raw).strip())
    except ValueError as exc:
        # A node *range* ("2-4") cannot be split into a fixed per-node GRES count.
        raise GpuSharingError(f"nodes={raw!r} is not a fixed node count") from exc
    if nodes < 1:
        raise GpuSharingError(f"nodes must be >= 1, got {raw!r}")
    return nodes


def _per_node_gpus(resources: dict[str, Any], gpus: int) -> int | None:
    """GPUs per node for a job of ``gpus`` in total, or ``None`` when they do not split evenly."""
    nodes = _nodes(resources)
    if gpus % nodes:
        return None
    return gpus // nodes


def _uneven_reason(resources: dict[str, Any], gpus: int) -> str:
    return (
        f"{gpus} GPU(s) do not split evenly over {_nodes(resources)} nodes, and Slurm's --gres "
        "is a per-node count"
    )


def map_to_scheduler(
    scheduler: str,
    resources: dict[str, Any],
    ask: FractionalAsk,
    caps: ClusterGpuCaps,
    *,
    gpus: int = 1,
) -> SchedulerGpuMapping:
    """Rewrite ``resources`` so ``scheduler`` allocates ``gpus`` × ``ask`` on this cluster.

    ``resources`` is not mutated. A whole-GPU ask (fraction 1, no MIG profile) passes through with
    ``gpus`` set and no warning, so the default path is exactly what it was.
    """
    scheduler = (scheduler or "").strip().lower()
    if scheduler not in SCHEDULERS:
        raise GpuSharingError(f"unknown scheduler {scheduler!r} (expected one of {SCHEDULERS})")
    _validate(ask, gpus)
    out = dict(resources)
    warnings: list[str] = []
    choice = select_mechanism(ask, caps)

    if scheduler == "mock":
        out["gpus"] = gpus
        return SchedulerGpuMapping(scheduler, out, choice, warnings)

    # Slurm's ``--gres`` is a PER-NODE count, whereas ``gpus`` here is the job total (the
    # adapter's ``--gpus``). A multi-node job must ask each node for its share, or it would get
    # ``nodes ×`` the slices it asked for. A total that does not split evenly is not expressible.
    per_node = _per_node_gpus(out, gpus) if scheduler == "slurm" else None

    if choice.mechanism == "mig":
        profile = choice.mig_profile or ""
        if scheduler == "slurm" and per_node is not None:
            gres_type = caps.mig_gres_types.get(profile, profile)
            out.pop("gpus", None)
            out.pop("gpus_per_node", None)
            out["gres"] = f"gpu:{gres_type}:{per_node}"
            return SchedulerGpuMapping(scheduler, out, choice, warnings)
        prop = caps.flux_mig_properties.get(profile)
        if scheduler == "flux" and prop and not out.get("constraint"):
            out["gpus"] = gpus
            out["constraint"] = prop
            return SchedulerGpuMapping(scheduler, out, choice, warnings)
        if scheduler == "slurm":
            reason = _uneven_reason(out, gpus)
        elif prop:
            reason = (
                f"flux cannot combine MIG property {prop!r} with the existing constraint "
                f"{out.get('constraint')!r}"
            )
        else:
            reason = f"flux cluster declares no node property exposing MIG {profile} slices"
        choice = _whole(ask, reason)

    elif choice.mechanism == "timeslice":
        if scheduler == "slurm" and caps.shards_per_gpu > 0 and per_node is not None:
            shards = max(
                1, math.ceil(per_node * ask.effective_fraction * caps.shards_per_gpu - 1e-9)
            )
            alloc = shards / (caps.shards_per_gpu * per_node)
            choice = MechanismChoice(
                "timeslice",
                "soft",
                alloc,
                ask.effective_fraction,
                max(0.0, alloc - ask.effective_fraction),
                f"Slurm gres/shard ×{shards} of {caps.shards_per_gpu}/GPU "
                "(SOFT isolation — no memory/SM partition)",
            )
            out.pop("gpus", None)
            out.pop("gpus_per_node", None)
            out["gres"] = f"shard:{shards}"
            return SchedulerGpuMapping(scheduler, out, choice, warnings)
        if scheduler == "slurm":
            reason = (
                "slurm cluster declares no shards_per_gpu"
                if caps.shards_per_gpu <= 0
                else _uneven_reason(out, gpus)
            )
        else:
            reason = "flux-core cannot express a time-sliced GPU"
        choice = _whole(ask, reason)

    out["gpus"] = gpus
    if choice.wasted_fraction > 1e-9:
        warnings.append(
            f"GPU sharing unavailable for this {scheduler} job: {choice.note}. "
            "Declare the mechanism in the cluster's capabilities (and, on Slurm, ask for a GPU "
            "total that divides evenly across the nodes) to share."
        )
    return SchedulerGpuMapping(scheduler, out, choice, warnings)


def ask_from_env(env: dict[str, str] | None = None) -> FractionalAsk | None:
    """The fractional ask a run subprocess was given, or ``None`` for a whole-GPU run."""
    env = dict(os.environ) if env is None else env
    raw_frac = (env.get(ENV_FRACTION) or "").strip()
    mig = (env.get(ENV_MIG_PROFILE) or "").strip() or None
    if not raw_frac and not mig:
        return None
    try:
        frac = float(raw_frac) if raw_frac else 1.0
    except ValueError as exc:
        raise GpuSharingError(f"{ENV_FRACTION}={raw_frac!r} is not a number") from exc
    ask = FractionalAsk("run", fraction=frac, mig_profile=mig)
    _validate(ask, 1)
    if ask.effective_fraction >= 1.0 and not mig:
        return None
    return ask


def caps_from_env(env: dict[str, str] | None = None) -> ClusterGpuCaps:
    """Cluster sharing capabilities exported by ``hpc_registry.resolve_env`` (else none)."""
    env = dict(os.environ) if env is None else env
    raw = (env.get(ENV_SHARING_CAPS) or "").strip()
    if not raw:
        return ClusterGpuCaps()
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise GpuSharingError(f"{ENV_SHARING_CAPS} is not valid JSON") from exc
    return caps_from_capabilities(parsed if isinstance(parsed, dict) else None)


def sharing_env_for_capabilities(capabilities: dict[str, Any] | None) -> dict[str, str]:
    """The ``EXAMLOPS_HPC_GPU_SHARING`` env for a cluster, empty when it declares no sharing."""
    caps = capabilities if isinstance(capabilities, dict) else {}
    subset = {k: caps[k] for k in SHARING_CAPABILITY_KEYS if k in caps}
    return {ENV_SHARING_CAPS: json.dumps(subset, sort_keys=True)} if subset else {}


def ask_env(fraction: float, mig_profile: str | None) -> dict[str, str]:
    """Env that carries a fractional ask into the run subprocess (empty for a whole GPU)."""
    env: dict[str, str] = {}
    if 0.0 < fraction < 1.0:
        env[ENV_FRACTION] = f"{fraction:g}"
    if mig_profile:
        env[ENV_MIG_PROFILE] = mig_profile
    return env


__all__ = [
    "ENV_FRACTION",
    "ENV_MIG_PROFILE",
    "ENV_SHARING_CAPS",
    "GpuSharingError",
    "SchedulerGpuMapping",
    "ask_env",
    "ask_from_env",
    "caps_from_env",
    "map_to_scheduler",
    "sharing_env_for_capabilities",
]

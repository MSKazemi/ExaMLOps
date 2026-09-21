"""Resume-cost model - a pure function that never invents a number (ADR 0109 decision 2/6)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from .types import Capability, PreemptionPromise, ResumeCost

_MB = 1024 * 1024
# preemption needs state that outlives the process, so an application checkpoint qualifies and
# a backend with no persistent tier or no state kinds does not.
_ORDER = {"unknown": 0, "declared": 1, "measured": 2}


def estimate_resume_cost(capability: Capability, state_size_bytes: int | None) -> ResumeCost:
    """Estimate ``state_transfer_s + communicator_rebuild_s`` for a restore of ``state_size_bytes``.

    Returns ``total_s=None`` with ``basis="unknown"`` whenever a needed input is missing - an
    unknown throughput, an unknown size, or a communicator that exists but was never measured.
    The basis of a known total is the weakest basis among its parts.
    """
    if state_size_bytes is None or state_size_bytes < 0:
        return ResumeCost(None, None, None, "unknown", "state size unknown")
    if capability.restore_throughput_mb_s is None or capability.restore_throughput_mb_s <= 0:
        return ResumeCost(None, None, None, "unknown", "restore throughput unknown")

    transfer = (capability.restore_fixed_s or 0.0) + (
        state_size_bytes / _MB / capability.restore_throughput_mb_s
    )
    if not capability.communicator_rebuild_applicable:
        rebuild = 0.0  # a fact of the mechanism (no communicator), not an estimate
    elif capability.communicator_rebuild_s is None:
        return ResumeCost(
            None, transfer, None, "unknown", "communicator rebuild applies but is unmeasured"
        )
    else:
        rebuild = capability.communicator_rebuild_s
    return ResumeCost(transfer + rebuild, transfer, rebuild, capability.basis)


def preemption_promise(capability: Capability) -> PreemptionPromise:
    """May a broker promise checkpoint-preserving preemption on this backend? (decision 3)

    It declines, and says why, rather than silently killing and restarting.
    """
    reasons: list[str] = []
    if not capability.state_kinds:
        reasons.append("backend persists no state kinds")
    if "persistent_storage" not in capability.tiers and "peer_memory" not in capability.tiers:
        reasons.append(
            "no tier survives loss of the compute (needs persistent_storage/peer_memory)"
        )
    if capability.granularity == "application":
        reasons.append(
            "application granularity: preserves only state the workload itself checkpoints "
            "(agent sessions), not a process or GPU image"
        )
    # The application-level reason narrows the promise rather than voiding it: an agent session is
    # exactly the workload it can preserve. A hard block is only the absence of durable state.
    hard = [r for r in reasons if not r.startswith("application granularity")]
    return PreemptionPromise(can_promise=not hard, reasons=tuple(reasons))


def with_measurements(capability: Capability, restores: Iterable[Mapping[str, Any]]) -> Capability:
    """Fold recorded restores into a capability, upgrading its basis to ``measured``.

    ``restores`` are rows with ``state_bytes`` and ``state_transfer_s``. With no usable row the
    capability is returned unchanged - measurement is never fabricated from nothing.
    """
    total_bytes = 0
    total_s = 0.0
    n = 0
    for r in restores:
        b, s = r.get("state_bytes"), r.get("state_transfer_s")
        if b is None or s is None or s <= 0:
            continue
        total_bytes += int(b)
        total_s += float(s)
        n += 1
    if n == 0 or total_bytes <= 0:
        return capability
    return replace(
        capability,
        restore_throughput_mb_s=(total_bytes / _MB) / total_s,
        restore_fixed_s=None,
        basis="measured",
        notes=(capability.notes + f" Throughput measured over {n} recorded restore(s).").strip(),
    )

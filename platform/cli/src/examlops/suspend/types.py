"""Typed records for the suspend/resume seam (ADR 0109).

Everything here is dependency-free and honest by construction: a number the backend does not know
is ``None`` with a ``basis`` of ``"unknown"``, never a default. ``basis`` is one of

* ``measured``  - observed on this platform (recorded restores);
* ``declared``  - stated by the backend or the operator, not observed here;
* ``unknown``   - nobody has said; consumers must not act as if a value existed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ADR 0109 decision 2 lists process | container | accelerator_state. The framework-level default
# (decision 4) checkpoints *application* state, which is none of those three, so it gets its own
# value rather than being mislabelled as a finer granularity than it has.
GRANULARITIES = ("application", "process", "container", "accelerator_state")
# ADR 0109 decision 8: tiers, not one granularity.
TIERS = ("local_memory", "peer_memory", "persistent_storage")
BASES = ("measured", "declared", "unknown")

STATE_AGENT_SESSION = "agent_session_checkpoint"


class SuspendError(Exception):
    """A suspend/resume operation failed."""


class SuspendUnsupported(SuspendError):
    """The backend cannot do what was asked. Raised instead of pretending it can."""


@dataclass(frozen=True)
class Capability:
    """What one backend can really do (ADR 0109 decision 2 and 8).

    ``restore_throughput_mb_s``, ``restore_fixed_s`` and ``communicator_rebuild_s`` are ``None``
    when unknown; ``basis`` says where the non-``None`` ones came from.
    ``communicator_rebuild_applicable`` is a *fact about the mechanism*: an application-level
    checkpoint has no NCCL/MPI communicator to rebuild, so its rebuild cost is exactly zero and
    that is not an estimate. An accelerator backend that has one and has not measured it leaves
    ``communicator_rebuild_s`` as ``None``.
    """

    backend: str
    granularity: str
    state_kinds: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    peer_replication: bool = False
    gpu_state: bool = False
    communicator_rebuild_applicable: bool = False
    communicator_rebuild_s: float | None = None
    restore_fixed_s: float | None = None
    restore_throughput_mb_s: float | None = None
    basis: str = "unknown"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.granularity not in GRANULARITIES:
            raise ValueError(
                f"granularity must be one of {GRANULARITIES}, got {self.granularity!r}"
            )
        if self.basis not in BASES:
            raise ValueError(f"basis must be one of {BASES}, got {self.basis!r}")
        bad = [t for t in self.tiers if t not in TIERS]
        if bad:
            raise ValueError(f"unknown tiers {bad}; expected a subset of {TIERS}")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state_kinds"] = list(self.state_kinds)
        d["tiers"] = list(self.tiers)
        return d


@dataclass(frozen=True)
class SnapshotHandle:
    """A durable reference to suspended state. ``pointer`` locates it; it is not the state."""

    snapshot_id: str
    backend: str
    subject_kind: str
    subject_id: str
    pointer: dict[str, Any] = field(default_factory=dict)
    state_bytes: int | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class RestoreReport:
    """The timing split of decision 6. ``None`` means not measured or not applicable."""

    restored: bool
    state_transfer_s: float | None
    communicator_rebuild_s: float | None
    detail: str = ""

    @property
    def total_s(self) -> float | None:
        parts = [p for p in (self.state_transfer_s, self.communicator_rebuild_s) if p is not None]
        return sum(parts) if parts else None


@dataclass(frozen=True)
class ResumeCost:
    """Estimated resume cost with its provenance. ``total_s is None`` => we do not know."""

    total_s: float | None
    state_transfer_s: float | None
    communicator_rebuild_s: float | None
    basis: str
    reason: str = ""


@dataclass(frozen=True)
class PreemptionPromise:
    """Whether a consumer may promise checkpoint-preserving preemption (decision 3)."""

    can_promise: bool
    reasons: tuple[str, ...] = ()

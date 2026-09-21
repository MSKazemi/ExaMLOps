"""``examlops.suspend`` - the suspend/resume seam (ADR 0109).

A pluggable ``SuspendBackend`` (snapshot / restore / discard / capability), an honest typed
``Capability`` (unknown stays ``None``), a pure resume-cost model, and durable audited records.
The only backend that ships that does real work is ``checkpoint-only`` (agent-session checkpoints
in the agent state store); there is no CRIU / GPU-state backend, by design.
"""

from __future__ import annotations

from .cost import estimate_resume_cost, preemption_promise, with_measurements
from .protocol import SuspendBackend
from .types import (
    STATE_AGENT_SESSION,
    Capability,
    PreemptionPromise,
    RestoreReport,
    ResumeCost,
    SnapshotHandle,
    SuspendError,
    SuspendUnsupported,
)

__all__ = [
    "STATE_AGENT_SESSION",
    "Capability",
    "PreemptionPromise",
    "RestoreReport",
    "ResumeCost",
    "SnapshotHandle",
    "SuspendBackend",
    "SuspendError",
    "SuspendUnsupported",
    "estimate_resume_cost",
    "preemption_promise",
    "with_measurements",
]

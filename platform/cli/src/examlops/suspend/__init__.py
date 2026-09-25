"""``examlops.suspend`` - the suspend/resume seam (ADR 0109).

A pluggable ``SuspendBackend`` (snapshot / restore / discard / capability), an honest typed
``Capability`` (unknown stays ``None``), a pure resume-cost model, and durable audited records.
The backends that ship do real work: ``checkpoint-only`` (agent-session checkpoints in the agent
state store), ``training-checkpoint`` (ADR 0032's sharded, hash-manifested training checkpoints),
``tiered-training-checkpoint`` (the same plus a verified node-local replica tier, decision 8) and
``vllm-sleep`` (engine-delegated vLLM sleep mode, decision 5). There is no CRIU / GPU-state
backend, by design.
"""

from __future__ import annotations

from .cost import estimate_resume_cost, preemption_promise, with_measurements
from .protocol import SuspendBackend
from .types import (
    STATE_AGENT_SESSION,
    STATE_SERVING_REPLICA,
    STATE_TRAINING_RUN,
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
    "STATE_SERVING_REPLICA",
    "STATE_TRAINING_RUN",
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

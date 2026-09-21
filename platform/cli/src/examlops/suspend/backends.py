"""Built-in suspend backends: ``checkpoint-only`` (default, real) and ``mock`` (tests).

There is deliberately **no** CRIU, ``cuda-checkpoint``, vLLM sleep/wake or peer-replication
backend: none can be exercised here, and a backend that only claims the capability is exactly the
failure ADR 0109 decision 2 forbids. A real one ships as an ``exa.providers.suspend_backend``
plugin whose ``capability()`` it can defend.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from examlops.providers import Provider, ProviderMeta

from .checkpoint_store import CheckpointStore, LangGraphSqliteStore
from .types import (
    STATE_AGENT_SESSION,
    Capability,
    RestoreReport,
    SnapshotHandle,
    SuspendError,
    SuspendUnsupported,
)


class _BackendBase(Provider):
    """A backend is also a ``Provider`` so the registry/CLI can list and describe it."""

    version = "1.0"

    def capability(self) -> Capability:  # pragma: no cover - abstract
        raise NotImplementedError

    def metadata(self) -> ProviderMeta:
        cap = self.capability()
        return ProviderMeta(
            methodology=cap.notes,
            outputs=("capability",),
            source="ADR 0109",
        )

    def compute(self, inputs: Any) -> dict[str, Any]:
        return {"capability": self.capability().as_dict()}

    @staticmethod
    def _check(cap: Capability, subject_kind: str, options: dict[str, Any] | None) -> None:
        opts = options or {}
        if subject_kind not in cap.state_kinds:
            raise SuspendUnsupported(
                f"backend {cap.backend!r} cannot persist {subject_kind!r} "
                f"(supports: {list(cap.state_kinds) or 'nothing'})"
            )
        if opts.get("require_gpu_state") and not cap.gpu_state:
            raise SuspendUnsupported(f"backend {cap.backend!r} does not support GPU state")
        want = opts.get("require_granularity")
        if want and want != cap.granularity:
            raise SuspendUnsupported(
                f"backend {cap.backend!r} has granularity {cap.granularity!r}, not {want!r}"
            )


class CheckpointOnlyBackend(_BackendBase):
    """Framework-level suspend (ADR 0109 decision 4): reuse the checkpoints the agent already writes.

    ``snapshot`` pins the newest LangGraph checkpoint of a thread (a pointer, never a copy);
    ``restore`` re-verifies that checkpoint still exists, reads its bytes and reports the measured
    read time. The state is actually loaded by LangGraph on the next invoke for that thread id -
    this backend does not, and cannot, load it into the agent.
    """

    name = "checkpoint-only"

    def __init__(self, store: CheckpointStore | None = None) -> None:
        self._store = store

    def _get_store(self) -> CheckpointStore:
        if self._store is not None:
            return self._store
        path = os.getenv("AGENT_DB")
        if not path:
            raise SuspendError("no agent checkpoint store configured (set AGENT_DB)")
        return LangGraphSqliteStore(path)

    def capability(self) -> Capability:
        return Capability(
            backend=self.name,
            granularity="application",
            state_kinds=(STATE_AGENT_SESSION,),
            tiers=("persistent_storage",),
            peer_replication=False,
            gpu_state=False,
            communicator_rebuild_applicable=False,
            communicator_rebuild_s=None,
            restore_fixed_s=None,
            restore_throughput_mb_s=None,
            basis="unknown",
            notes=(
                "Application-level checkpoint of an agent session in the agent state store "
                "(SQLite). No process, container or GPU image. Restore time is unknown until "
                "restores have been recorded."
            ),
        )

    def snapshot(
        self, subject_kind: str, subject_id: str, *, options: dict[str, Any] | None = None
    ) -> SnapshotHandle:
        self._check(self.capability(), subject_kind, options)
        store = self._get_store()
        ref = store.latest(subject_id)
        if ref is None:
            raise SuspendError(
                f"no checkpoint exists for thread {subject_id!r}; nothing to suspend"
            )
        return SnapshotHandle(
            snapshot_id=uuid.uuid4().hex,
            backend=self.name,
            subject_kind=subject_kind,
            subject_id=subject_id,
            pointer={"checkpoint_ns": ref.checkpoint_ns, "checkpoint_id": ref.checkpoint_id},
            state_bytes=ref.size_bytes,
            created_at=time.time(),
        )

    def restore(self, handle: SnapshotHandle) -> RestoreReport:
        store = self._get_store()
        t0 = time.perf_counter()
        ref = store.get(
            handle.subject_id,
            str(handle.pointer.get("checkpoint_ns", "")),
            str(handle.pointer.get("checkpoint_id", "")),
        )
        if ref is None:
            return RestoreReport(False, None, None, "pinned checkpoint no longer in the store")
        blob = store.read_blob(ref)
        elapsed = time.perf_counter() - t0
        if handle.state_bytes is not None and len(blob) != handle.state_bytes:
            return RestoreReport(False, None, None, "checkpoint size changed since snapshot")
        return RestoreReport(
            True,
            elapsed,
            0.0,  # no communicator exists for an application checkpoint
            "verified and read the pinned checkpoint; LangGraph loads it on the next invoke",
        )

    def discard(self, handle: SnapshotHandle) -> None:
        # The checkpoint belongs to the agent runtime and is not ours to delete; only the
        # suspend record (kept by the service) is released.
        return None


class MockBackend(_BackendBase):
    """In-memory backend for tests and for exercising consumers' degrade paths.

    Its ``Capability`` is whatever the test constructs, so a consumer can be shown a process-level
    or GPU-less backend without any hardware. It claims nothing about real systems.
    """

    name = "mock"
    _STATE: dict[str, bytes] = {}  # shared: the registry builds a fresh instance per resolution

    def __init__(self, cap: Capability | None = None) -> None:
        self._cap = cap or Capability(
            backend="mock",
            granularity="application",
            state_kinds=(STATE_AGENT_SESSION,),
            tiers=("local_memory",),
            basis="declared",
            notes="In-memory test double. Declares nothing measurable.",
        )
        self._state = MockBackend._STATE

    def capability(self) -> Capability:
        return self._cap

    def snapshot(
        self, subject_kind: str, subject_id: str, *, options: dict[str, Any] | None = None
    ) -> SnapshotHandle:
        self._check(self._cap, subject_kind, options)
        data = str((options or {}).get("state", subject_id)).encode()
        sid = uuid.uuid4().hex
        self._state[sid] = data
        return SnapshotHandle(
            sid, self.name, subject_kind, subject_id, {"key": sid}, len(data), time.time()
        )

    def restore(self, handle: SnapshotHandle) -> RestoreReport:
        ok = handle.snapshot_id in self._state
        return RestoreReport(ok, 0.0 if ok else None, None, "" if ok else "unknown snapshot")

    def discard(self, handle: SnapshotHandle) -> None:
        self._state.pop(handle.snapshot_id, None)

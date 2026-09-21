"""The seam's contract (ADR 0109 decision 1): snapshot / restore / discard / capability."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .types import Capability, RestoreReport, SnapshotHandle


@runtime_checkable
class SuspendBackend(Protocol):
    """A pluggable suspend mechanism. Register one under the ``suspend_backend`` provider domain."""

    name: str

    def capability(self) -> Capability:
        """What this backend can genuinely do. Must not overstate."""
        ...

    def snapshot(
        self, subject_kind: str, subject_id: str, *, options: dict[str, Any] | None = None
    ) -> SnapshotHandle:
        """Persist (or pin) the subject's state. Raise ``SuspendUnsupported`` if it cannot."""
        ...

    def restore(self, handle: SnapshotHandle) -> RestoreReport:
        """Make the state loadable again and report the timing split (decision 6)."""
        ...

    def discard(self, handle: SnapshotHandle) -> None:
        """Release this backend's claim on the snapshot."""
        ...

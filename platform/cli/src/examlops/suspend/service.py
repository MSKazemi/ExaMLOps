"""The suspend/resume service (ADR 0109): snapshot / restore / discard / capability + records.

Nothing calls this by default. A backend is chosen by the ``suspend_backend`` provider domain
(``EXAMLOPS_SUSPEND_BACKEND_PROVIDER`` or an explicit name); an unknown or unregistered name is a
refusal, never a silent fallback to a backend that cannot do the job.
"""

from __future__ import annotations

import getpass
from typing import Any

from examlops.data import suspend as store
from examlops.data.audit import audit_best_effort
from examlops.providers import ProviderError
from examlops.providers.loader import resolve_provider

from . import providers as _providers  # noqa: F401  (registers the built-ins)
from .cost import with_measurements
from .protocol import SuspendBackend
from .types import (
    STATE_AGENT_SESSION,
    Capability,
    RestoreReport,
    SnapshotHandle,
    SuspendError,
    SuspendUnsupported,
)

_SOURCE = "suspend"


def _actor(actor: str | None) -> str:
    if actor:
        return actor
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def get_backend(name: str | None = None) -> SuspendBackend:
    """Resolve a backend, refusing (not faking) one that is not registered."""
    try:
        backend = resolve_provider(_providers.DOMAIN, override=name, group="platform", config={})
    except ProviderError as exc:
        raise SuspendUnsupported(
            f"no suspend backend {name!r} is registered ({exc}). "
            "Hardware backends (CRIU, cuda-checkpoint, ...) are not built in; ship one as an "
            "'exa.providers.suspend_backend' plugin."
        ) from exc
    if not isinstance(backend, SuspendBackend):
        raise SuspendUnsupported(f"provider {name!r} does not implement the SuspendBackend seam")
    return backend


def capability(name: str | None = None) -> Capability:
    """The backend's honest capability, upgraded to ``measured`` only from recorded restores."""
    backend = get_backend(name)
    cap = backend.capability()
    return with_measurements(cap, store.recorded_restores(cap.backend))


def suspend(
    subject_id: str,
    *,
    subject_kind: str = STATE_AGENT_SESSION,
    backend: str | None = None,
    options: dict[str, Any] | None = None,
    tenant: str = "default",
    actor: str | None = None,
) -> SnapshotHandle:
    """Snapshot ``subject_id`` and record it. Refusals are audited and re-raised."""
    who = _actor(actor)
    b = get_backend(backend)
    try:
        handle = b.snapshot(subject_kind, subject_id, options=options)
    except SuspendError as exc:
        audit_best_effort(
            _SOURCE,
            who,
            "suspend_refused",
            subject_id,
            {"backend": b.name, "reason": str(exc)},
            tenant=tenant,
        )
        raise
    store.put(
        handle.snapshot_id,
        handle.backend,
        handle.subject_kind,
        handle.subject_id,
        tenant=tenant,
        pointer=handle.pointer,
        state_bytes=handle.state_bytes,
        capability=b.capability().as_dict(),
        actor=who,
    )
    audit_best_effort(
        _SOURCE,
        who,
        "suspend_snapshot",
        subject_id,
        {"snapshot_id": handle.snapshot_id, "backend": handle.backend, "bytes": handle.state_bytes},
        tenant=tenant,
    )
    return handle


def _handle(row: dict[str, Any]) -> SnapshotHandle:
    return SnapshotHandle(
        row["snapshot_id"],
        row["backend"],
        row["subject_kind"],
        row["subject_id"],
        row.get("pointer") or {},
        row.get("state_bytes"),
        row["ts"],
    )


def resume(snapshot_id: str, *, actor: str | None = None) -> RestoreReport:
    """Restore a suspended snapshot and record the timing split."""
    row = store.get(snapshot_id)
    if row is None:
        raise SuspendError(f"unknown snapshot {snapshot_id!r}")
    if row["status"] != "suspended":
        raise SuspendError(f"snapshot {snapshot_id!r} is {row['status']}, not suspended")
    who = _actor(actor)
    try:
        report = get_backend(row["backend"]).restore(_handle(row))
    except SuspendError as exc:
        # The backend could not decide (e.g. its engine was unreachable): the record stays
        # ``suspended`` so the resume can be retried, and the attempt is still audited.
        audit_best_effort(
            _SOURCE,
            who,
            "suspend_resume_error",
            row["subject_id"],
            {"snapshot_id": snapshot_id, "backend": row["backend"], "reason": str(exc)},
            tenant=row["tenant"],
        )
        raise
    if report.restored:
        store.mark(
            snapshot_id,
            "resumed",
            state_transfer_s=report.state_transfer_s,
            communicator_rebuild_s=report.communicator_rebuild_s,
        )
    else:
        store.mark(snapshot_id, "failed")
    audit_best_effort(
        _SOURCE,
        who,
        "suspend_resume" if report.restored else "suspend_resume_failed",
        row["subject_id"],
        {
            "snapshot_id": snapshot_id,
            "backend": row["backend"],
            "state_transfer_s": report.state_transfer_s,
            "communicator_rebuild_s": report.communicator_rebuild_s,
            "detail": report.detail,
        },
        tenant=row["tenant"],
    )
    return report


def discard(snapshot_id: str, *, actor: str | None = None) -> None:
    row = store.get(snapshot_id)
    if row is None:
        raise SuspendError(f"unknown snapshot {snapshot_id!r}")
    get_backend(row["backend"]).discard(_handle(row))
    store.mark(snapshot_id, "discarded")
    audit_best_effort(
        _SOURCE,
        _actor(actor),
        "suspend_discard",
        row["subject_id"],
        {"snapshot_id": snapshot_id},
        tenant=row["tenant"],
    )


def status(snapshot_id: str) -> dict[str, Any] | None:
    return store.get(snapshot_id)


def list_snapshots(
    subject_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    *,
    tenant: str | None = None,
):
    return store.list_snapshots(subject_id, status, limit, tenant=tenant)

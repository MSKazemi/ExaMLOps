"""The seam's current ceiling, as data (ADR 0109 consequence: "reported in ``exa status``").

The ADR's mitigation for "a seam with only a weak backend can look like completed work" is that
``capability()`` and the restore timing split are always visible. :func:`seam_report` is that
view: every registered backend's honest capability (upgraded to ``measured`` only from recorded
restores), whether a broker may promise checkpoint-preserving preemption on it and why not, and
the timing split of its most recent restores. It never raises - an unreadable store or a backend
that fails to describe itself becomes an ``error`` field, so a status page cannot be taken down
by the seam it is reporting on.
"""

from __future__ import annotations

import os
from statistics import median
from typing import Any

from examlops.data import suspend as store
from examlops.providers import list_providers
from examlops.providers.registry import default_provider_name

from . import providers as _providers
from .cost import preemption_promise, with_measurements
from .protocol import SuspendBackend

_RECENT = 20


def _split(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Median/last of each half of the split; ``None`` where no restore recorded it."""

    def col(name: str) -> list[float]:
        return [float(r[name]) for r in rows if r.get(name) is not None]

    transfer, rebuild = col("state_transfer_s"), col("communicator_rebuild_s")
    last = rows[0] if rows else {}
    return {
        "restores": len(rows),
        "state_transfer_s_median": median(transfer) if transfer else None,
        "communicator_rebuild_s_median": median(rebuild) if rebuild else None,
        "last_state_transfer_s": last.get("state_transfer_s"),
        "last_communicator_rebuild_s": last.get("communicator_rebuild_s"),
    }


def backend_report(backend: SuspendBackend) -> dict[str, Any]:
    cap = backend.capability()
    try:
        cap = with_measurements(cap, store.recorded_restores(cap.backend))
        timing = _split(store.recent_restores(cap.backend, _RECENT))
    except Exception as exc:  # noqa: BLE001 - a status view degrades, it does not crash
        timing = {"restores": None, "error": f"suspend store unreadable: {exc}"}
    promise = preemption_promise(cap)
    return {
        "backend": cap.backend,
        "capability": cap.as_dict(),
        "preemption": {"can_promise": promise.can_promise, "reasons": list(promise.reasons)},
        "timing": timing,
    }


def seam_report(tenant: str | None = None) -> dict[str, Any]:
    """Every registered ``suspend_backend`` with capability, preemption verdict and timing."""
    selected = os.getenv(f"EXAMLOPS_{_providers.DOMAIN.upper()}_PROVIDER") or default_provider_name(
        _providers.DOMAIN
    )
    backends: list[dict[str, Any]] = []
    for info in list_providers(_providers.DOMAIN):
        if not info.ok or info.provider is None:
            backends.append({"backend": info.name, "error": info.error or "failed to load"})
            continue
        if not isinstance(info.provider, SuspendBackend):
            backends.append({"backend": info.name, "error": "does not implement SuspendBackend"})
            continue
        try:
            backends.append(backend_report(info.provider))
        except Exception as exc:  # noqa: BLE001
            backends.append({"backend": info.name, "error": str(exc)})
    try:
        snapshots: dict[str, Any] = store.count_by_status(tenant)
    except Exception as exc:  # noqa: BLE001
        snapshots = {"error": f"suspend store unreadable: {exc}"}
    return {
        "selected_backend": selected,
        "backends": sorted(backends, key=lambda b: str(b.get("backend"))),
        "snapshots": snapshots,
    }

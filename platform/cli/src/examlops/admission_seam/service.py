"""Build the seam's inputs from the live platform and take audited decisions."""

from __future__ import annotations

from typing import Any

from examlops.data import quota_reservations as store
from examlops.data.audit import audit_best_effort

from .capability import AdapterCapabilities
from .policy import (
    DEFAULT_POLICY,
    ClusterState,
    Decision,
    Quotas,
    ignored_fields,
    quotas_from_env,
    select_policy,
)
from .request import JobRequest


def current_state(capabilities: AdapterCapabilities | None = None) -> ClusterState:
    """Running counts from the admission queue, held GPUs from reservations, capacity from the
    fleet inventory (``None`` when no node snapshot exists - unknown, never assumed)."""
    running = store.admission_running_counts()
    total = free = None
    try:
        from examlops.hpc_capacity import capacity_summary

        summary = capacity_summary(ttl=0)
        if summary:
            total = sum(int(c.get("total_gpus") or 0) for c in summary.values())
            free = sum(int(c.get("idle_gpus") or 0) for c in summary.values())
    except Exception:  # noqa: BLE001 - no inventory means unknown capacity, not a failed decision
        total = free = None
    return ClusterState(
        running_total=running["total"],
        running_by_tenant=running["by_tenant"],
        gpus_in_use_by_tenant=store.held_gpus_by_tenant(),
        total_gpus=total,
        free_gpus=free,
        capabilities=capabilities or AdapterCapabilities(),
    )


def decide(
    request: JobRequest,
    *,
    state: ClusterState | None = None,
    quotas: Quotas | None = None,
    policy: str | None = None,
    record: bool = True,
    actor: str | None = None,
) -> tuple[Decision, dict[str, Any]]:
    """One admission decision. Returns ``(decision, meta)``.

    A decision taken by a **non-default** policy is audited (``admission_decision``) so the log
    shows that something other than the historical fair-share ruled; the default path writes
    nothing new, keeping existing behaviour identical.
    """
    request.validate()
    pol = select_policy(policy)
    st = state if state is not None else current_state()
    qs = quotas if quotas is not None else quotas_from_env()
    decision = pol.decide(request, st, qs)
    meta = {
        "policy": pol.name,
        "ignored_fields": ignored_fields(pol, request),
        "capabilities": st.capabilities.to_dict(),
    }
    if record and pol.name != DEFAULT_POLICY:
        audit_best_effort(
            "admission-seam",
            actor,
            "admission_decision",
            request.project,
            {"policy": pol.name, "tenant": request.tenant, **decision.to_dict()},
            tenant=request.tenant,
        )
    return decision, meta

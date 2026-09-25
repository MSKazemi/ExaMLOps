"""Build the seam's inputs from the live platform and take audited decisions."""

from __future__ import annotations

from typing import Any

from examlops.data import quota_reservations as store
from examlops.data.audit import audit_best_effort

from . import gates as _gates
from .capability import AdapterCapabilities
from .policy import (
    DEFAULT_POLICY,
    Admit,
    ClusterState,
    Decision,
    Queue,
    Quotas,
    Reject,
    ignored_fields,
    quotas_from_env,
    select_policy,
)
from .request import JobRequest
from .topology import live_largest_free_domain_gpus


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
        # Declared scale-up topology over the node inventory (ADR 0116 decision 6); None when
        # the site has declared none, which the policy treats as unknown, never as "fits".
        largest_free_domain_gpus=live_largest_free_domain_gpus(),
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
    gate_names: list[str] | None = None,
) -> tuple[Decision, dict[str, Any]]:
    """One admission decision. Returns ``(decision, meta)``.

    A decision taken by a **non-default** policy is audited (``admission_decision``) so the log
    shows that something other than the historical fair-share ruled; the default path writes
    nothing new, keeping existing behaviour identical.

    ``gate_names`` overrides ``EXAMLOPS_ADMISSION_GATES`` for this call (``[]`` = no gates). A
    decision changed by a gate is audited as ``admission_gate_blocked`` when ``record`` is set.
    """
    request.validate()
    pol = select_policy(policy)
    st = state if state is not None else current_state()
    qs = quotas if quotas is not None else quotas_from_env()
    decision = pol.decide(request, st, qs)
    meta: dict[str, Any] = {
        "policy": pol.name,
        "ignored_fields": ignored_fields(pol, request),
        "capabilities": st.capabilities.to_dict(),
    }
    # External gates (ADR 0116 decision 5) run only on a request the policy would admit: a
    # queued or rejected request is already not running, and asking the budget store or the grid
    # feed about it would cost a lookup to say nothing. No gate configured -> nothing runs.
    if isinstance(decision, Admit) and (gate_names is not None or _gates.gates_configured()):
        results = _gates.evaluate_gates(request, names=gate_names, record=record)
        meta["gates"] = [r.to_dict() for r in results]
        blocking = _gates.combine(results)
        if blocking is not None:
            reason = f"gate {blocking.gate}: {blocking.reason}"
            decision = Reject(reason) if blocking.verdict == _gates.DENY else Queue(reason)
            if record:
                audit_best_effort(
                    "admission-seam",
                    actor,
                    "admission_gate_blocked",
                    request.project,
                    {
                        "tenant": request.tenant,
                        "policy": pol.name,
                        **decision.to_dict(),
                        "gates": meta["gates"],
                    },
                    tenant=request.tenant,
                )
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

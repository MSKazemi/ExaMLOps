"""The admission seam: ``decide(request, cluster_state, quotas)`` (ADR 0116 decision 1, 4).

Two policies sit behind :class:`AdmissionPolicy`:

* :class:`LegacyFairShare` (default, ``fair-share``) - the existing ``claim_next_admission`` logic
  re-expressed as pure functions: a global concurrency cap, a per-tenant cap and max-min fairness
  across tenants. ``tests/unit/test_admission_seam_policy.py`` proves it picks exactly what the
  SQL implementation picks, over a table of scenarios plus a randomised sweep.
* :class:`BaselineOverQuota` (``baseline-over-quota``) - the KAI/HyperPod shape: a per-tenant GPU
  *baseline* (guaranteed), an *over-quota* weight (borrowing idle capacity), and a hard *limit*.

Not built, and said so in the decision's reason text rather than implied: reclamation (taking a
borrowed GPU back), preemption, and time-based fairness. A baseline request that finds no free
GPU is QUEUED with that reason - it does not preempt anyone.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .capability import AdapterCapabilities
from .request import JobRequest

DEFAULT_POLICY = "fair-share"
POLICY_ENV = "EXAMLOPS_ADMISSION_POLICY"


# ── decisions ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Admit:
    reason: str = "admitted"
    #: Admitted on borrowed capacity beyond the tenant's baseline (revocable in a fuller design).
    over_quota: bool = False
    verdict: str = "admit"

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "over_quota": self.over_quota}


@dataclass(frozen=True)
class Queue:
    reason: str
    verdict: str = "queue"

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}


@dataclass(frozen=True)
class Reject:
    reason: str
    verdict: str = "reject"

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}


Decision = Admit | Queue | Reject


# ── inputs ───────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TenantQuota:
    baseline_gpus: int = 0
    limit_gpus: int | None = None
    over_quota_weight: float = 1.0


@dataclass(frozen=True)
class Quotas:
    max_running: int = 4
    per_tenant_cap: int = 2
    tenants: Mapping[str, TenantQuota] = field(default_factory=dict)

    def for_tenant(self, tenant: str) -> TenantQuota:
        return self.tenants.get(tenant, TenantQuota())


@dataclass(frozen=True)
class ClusterState:
    running_total: int = 0
    running_by_tenant: Mapping[str, int] = field(default_factory=dict)
    #: GPUs each tenant currently holds (running work + held reservations).
    gpus_in_use_by_tenant: Mapping[str, int] = field(default_factory=dict)
    total_gpus: int | None = None
    free_gpus: int | None = None
    #: Largest single scale-up domain's free GPUs; ``None`` = topology unknown.
    largest_free_domain_gpus: int | None = None
    capabilities: AdapterCapabilities = field(default_factory=AdapterCapabilities)


@dataclass(frozen=True)
class QueuedItem:
    id: int
    tenant: str
    priority: int = 0
    gpus: int = 0


# ── the seam ─────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class AdmissionPolicy(Protocol):
    name: str

    def decide(self, request: JobRequest, state: ClusterState, quotas: Quotas) -> Decision: ...

    def pick_next(
        self, queued: Sequence[QueuedItem], state: ClusterState, quotas: Quotas
    ) -> QueuedItem | None: ...


def legacy_pick(
    queued: Sequence[QueuedItem],
    running_by_tenant: Mapping[str, int],
    max_running: int,
    per_tenant_cap: int,
) -> QueuedItem | None:
    """``claim_next_admission``'s choice, without the database.

    Best queued item per tenant (priority desc, oldest id first); tenants at their cap or a full
    cluster are ineligible; among the rest, fewest running for the tenant, then priority, then id.
    """
    if sum(running_by_tenant.values()) >= max_running:
        return None
    best: dict[str, QueuedItem] = {}
    for it in sorted(queued, key=lambda i: (i.tenant, -i.priority, i.id)):
        best.setdefault(it.tenant, it)
    eligible = [it for t, it in best.items() if running_by_tenant.get(t, 0) < per_tenant_cap]
    if not eligible:
        return None
    return min(eligible, key=lambda i: (running_by_tenant.get(i.tenant, 0), -i.priority, i.id))


class LegacyFairShare:
    """Default policy: the pre-seam fair-share behaviour, unchanged.

    It knows nothing of gang, network tier, scale-up domain, deadlines or GPU quotas, exactly as
    the queue it re-expresses does; :func:`ignored_fields` reports what it did not look at.
    """

    name = "fair-share"

    def decide(self, request: JobRequest, state: ClusterState, quotas: Quotas) -> Decision:
        total = state.running_total
        if total >= quotas.max_running:
            return Queue(f"global concurrency cap reached ({total}/{quotas.max_running})")
        running = state.running_by_tenant.get(request.tenant, 0)
        if running >= quotas.per_tenant_cap:
            return Queue(
                f"tenant {request.tenant!r} at its concurrency cap "
                f"({running}/{quotas.per_tenant_cap})"
            )
        return Admit()

    def pick_next(
        self, queued: Sequence[QueuedItem], state: ClusterState, quotas: Quotas
    ) -> QueuedItem | None:
        return legacy_pick(
            queued, state.running_by_tenant, quotas.max_running, quotas.per_tenant_cap
        )


class BaselineOverQuota:
    """KAI/HyperPod-shaped GPU quota: baseline guaranteed, over-quota borrowed, limit hard."""

    name = "baseline-over-quota"

    def decide(self, request: JobRequest, state: ClusterState, quotas: Quotas) -> Decision:
        gpus = request.resources.gpus
        q = quotas.for_tenant(request.tenant)
        caps = state.capabilities

        if state.running_total >= quotas.max_running:
            return Queue(
                f"global concurrency cap reached ({state.running_total}/{quotas.max_running})"
            )
        if request.gang and caps.supports_gang is not True:
            said = "does not support" if caps.supports_gang is False else "has not declared"
            return Reject(f"gang scheduling requested but the backend {said} it; not promised")
        if request.scale_up_domain == "required":
            free_dom = state.largest_free_domain_gpus
            if free_dom is None:
                return Reject(
                    "scale_up_domain 'required' but the fabric topology is unknown; "
                    "cannot guarantee a single domain"
                )
            if free_dom < gpus:
                return Queue(
                    f"no single scale-up domain has {gpus} free GPUs (largest {free_dom}); "
                    "not placed spanning domains"
                )
        if state.total_gpus is not None and gpus > state.total_gpus:
            return Reject(f"asks {gpus} GPUs; the cluster has {state.total_gpus}")
        if q.limit_gpus is not None and gpus > q.limit_gpus:
            return Reject(f"asks {gpus} GPUs; tenant limit is {q.limit_gpus}")

        used = state.gpus_in_use_by_tenant.get(request.tenant, 0)
        if q.limit_gpus is not None and used + gpus > q.limit_gpus:
            return Queue(f"tenant at its GPU limit ({used}+{gpus} > {q.limit_gpus})")
        if state.free_gpus is not None and gpus > state.free_gpus:
            within = used + gpus <= q.baseline_gpus
            tail = (
                " (within baseline, but reclamation of borrowed GPUs is not implemented)"
                if within
                else ""
            )
            return Queue(f"insufficient free GPUs ({state.free_gpus} free, {gpus} asked){tail}")
        if used + gpus <= q.baseline_gpus:
            return Admit(f"within baseline ({used}+{gpus} <= {q.baseline_gpus})")
        return Admit(
            f"over-quota on idle capacity ({used}+{gpus} > baseline {q.baseline_gpus})",
            over_quota=True,
        )

    def pick_next(
        self, queued: Sequence[QueuedItem], state: ClusterState, quotas: Quotas
    ) -> QueuedItem | None:
        """Baseline claimants first; among borrowers, least used per unit of weight; then
        priority, then oldest. Only items the tenant limit allows are considered."""
        ranked: list[tuple[tuple[int, float, int, int], QueuedItem]] = []
        for it in queued:
            q = quotas.for_tenant(it.tenant)
            used = state.gpus_in_use_by_tenant.get(it.tenant, 0)
            if q.limit_gpus is not None and used + it.gpus > q.limit_gpus:
                continue
            if state.free_gpus is not None and it.gpus > state.free_gpus:
                continue
            borrowing = 0 if used + it.gpus <= q.baseline_gpus else 1
            share = used / max(q.over_quota_weight, 1e-9)
            ranked.append(((borrowing, share, -it.priority, it.id), it))
        if not ranked:
            return None
        return min(ranked, key=lambda r: r[0])[1]


_POLICIES: dict[str, type] = {
    LegacyFairShare.name: LegacyFairShare,
    BaselineOverQuota.name: BaselineOverQuota,
}


def policy_names() -> list[str]:
    return sorted(_POLICIES)


def select_policy(name: str | None = None) -> AdmissionPolicy:
    """The configured policy. Default ``fair-share``; an unknown name is refused, not defaulted."""
    chosen = (name or os.getenv(POLICY_ENV) or DEFAULT_POLICY).strip().lower()
    if chosen not in _POLICIES:
        raise ValueError(f"unknown admission policy {chosen!r}; choose one of {policy_names()}")
    return _POLICIES[chosen]()  # type: ignore[no-any-return]


def ignored_fields(policy: AdmissionPolicy, request: JobRequest) -> list[str]:
    """Request fields set to a non-default value that ``policy`` does not look at."""
    out: list[str] = []
    if policy.name == LegacyFairShare.name:
        for name, is_set in (
            ("gang", request.gang),
            ("network_tier", request.network_tier != "scale_out"),
            ("scale_up_domain", request.scale_up_domain != "not_required"),
            ("resources.gpus", request.resources.gpus > 0),
            ("priority_class", request.priority_class != "batch"),
        ):
            if is_set:
                out.append(name)
    # No shipped policy evaluates these yet.
    for name, is_set in (
        ("flexibility_s", request.flexibility_s > 0),
        ("deadline", request.deadline is not None),
        ("queue", request.queue is not None),
    ):
        if is_set:
            out.append(name)
    return out


def quotas_from_env() -> Quotas:
    """Caps from the existing env vars; per-tenant GPU quotas from ``EXAMLOPS_ADMISSION_QUOTAS``
    (a JSON file ``{"tenants": {"t": {"baseline_gpus": 4, "limit_gpus": 8,
    "over_quota_weight": 2}}}``). A malformed file raises: silently ignoring quotas would admit
    on no limits at all."""
    import json
    from pathlib import Path

    from examlops import admission

    tenants: dict[str, TenantQuota] = {}
    path = os.getenv("EXAMLOPS_ADMISSION_QUOTAS")
    if path:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        for name, spec in (doc.get("tenants") or {}).items():
            tenants[name] = TenantQuota(
                baseline_gpus=int(spec.get("baseline_gpus", 0)),
                limit_gpus=(
                    int(spec["limit_gpus"]) if spec.get("limit_gpus") is not None else None
                ),
                over_quota_weight=float(spec.get("over_quota_weight", 1.0)),
            )
    return Quotas(
        max_running=admission.max_running(),
        per_tenant_cap=admission.per_tenant_cap(),
        tenants=tenants,
    )

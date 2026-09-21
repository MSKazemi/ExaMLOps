"""Admission seam above the execution seam (ADR 0116).

``JobRequest`` -> [ AdmissionPolicy.decide ] -> Admit | Queue | Reject -> [ SchedulerAdapter ].
Additive: nothing in the existing queue or scheduler adapters imports this package, and the
default policy reproduces the pre-seam fair-share decisions exactly.
"""

from __future__ import annotations

from .capability import (
    AdapterCapabilities,
    PreemptCapable,
    PreemptUnsupported,
    preempt,
    probe,
    register_capabilities,
)
from .kueue import KueueUnsupported, render_kueue
from .policy import (
    AdmissionPolicy,
    Admit,
    BaselineOverQuota,
    ClusterState,
    LegacyFairShare,
    Queue,
    QueuedItem,
    Quotas,
    Reject,
    TenantQuota,
    legacy_pick,
    select_policy,
)
from .request import JobRequest, JobRequestError, Resources

__all__ = [
    "AdapterCapabilities",
    "Admit",
    "AdmissionPolicy",
    "BaselineOverQuota",
    "ClusterState",
    "JobRequest",
    "JobRequestError",
    "KueueUnsupported",
    "LegacyFairShare",
    "PreemptCapable",
    "PreemptUnsupported",
    "Queue",
    "QueuedItem",
    "Quotas",
    "Reject",
    "Resources",
    "TenantQuota",
    "legacy_pick",
    "preempt",
    "probe",
    "register_capabilities",
    "render_kueue",
    "select_policy",
]

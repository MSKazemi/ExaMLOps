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
from .completion import (
    OUTCOMES,
    holder_for_job,
    holder_for_run,
    normalize_outcome,
    on_job_terminal_state,
    release_on_completion,
)
from .dispatch import (
    AdmissionRefused,
    admitted,
    request_for_hpc_job,
    request_for_pipeline_run,
    submit_admitted,
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
    "OUTCOMES",
    "AdapterCapabilities",
    "AdmissionPolicy",
    "AdmissionRefused",
    "Admit",
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
    "admitted",
    "holder_for_job",
    "holder_for_run",
    "legacy_pick",
    "normalize_outcome",
    "on_job_terminal_state",
    "preempt",
    "probe",
    "register_capabilities",
    "release_on_completion",
    "render_kueue",
    "request_for_hpc_job",
    "request_for_pipeline_run",
    "select_policy",
    "submit_admitted",
]

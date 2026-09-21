"""Offline (batch) inference - a mode of every servable (ADR 0149). See :mod:`.executor`."""

from examlops.offline.executor import cancel, list_jobs, operation_view, run, status
from examlops.offline.spec import (
    INPUT_TYPES,
    KINDS,
    OUTPUT_TYPES,
    SCHEMA_VERSION,
    InputSpec,
    OfflineJob,
    OfflineSpecError,
    OutputSpec,
    Resources,
    job_id_for,
)

__all__ = [
    "INPUT_TYPES",
    "KINDS",
    "OUTPUT_TYPES",
    "SCHEMA_VERSION",
    "InputSpec",
    "OfflineJob",
    "OfflineSpecError",
    "OutputSpec",
    "Resources",
    "cancel",
    "job_id_for",
    "list_jobs",
    "operation_view",
    "run",
    "status",
]

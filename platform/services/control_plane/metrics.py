# services/control_plane/metrics.py
from __future__ import annotations

from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram

approvals_pending: Gauge = Gauge(
    "examlops_approvals_pending",
    "Current number of pending model change approvals",
)
approval_events: Counter = Counter(
    "examlops_approval_events_total",
    "Cumulative approval lifecycle events",
    ["model_id", "action"],
)
approval_age_oldest: Gauge = Gauge(
    "examlops_approval_age_oldest_seconds",
    "Age in seconds of the oldest pending approval; 0 when none pending",
)

# Improvement 4 (round 1): Prefect gateway retry counter
prefect_retries: Counter = Counter(
    "examlops_prefect_retries_total",
    "Number of retried Prefect HTTP calls (transient errors / 5xx)",
    ["method"],
)

# Improvement 12 (round 2): Circuit breaker state transitions
circuit_breaker_opens: Counter = Counter(
    "examlops_prefect_circuit_breaker_opens_total",
    "Number of times the Prefect circuit breaker transitioned to OPEN state",
)

# Improvement 13 (round 2): Retrain lifecycle metrics
retrain_requests: Counter = Counter(
    "examlops_retrain_requests_total",
    "Total retrain requests by model, dataset, and outcome",
    ["model_name", "dataset_name", "outcome"],  # outcome: success | error | dedup
)
retrain_duration: Histogram = Histogram(
    "examlops_retrain_duration_seconds",
    "Wall-clock time from POST /retrain to Prefect flow-run creation",
    ["model_name", "dataset_name"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

# Improvement 16 (round 2): Approval expiry
approvals_expired: Counter = Counter(
    "examlops_approvals_expired_total",
    "Total pending approvals that were auto-expired by the background cleanup",
)


def record_created(model_id: str, pending_count: int) -> None:
    approval_events.labels(model_id=model_id, action="created").inc()
    approvals_pending.set(pending_count)


def record_approved(model_id: str, pending_count: int) -> None:
    approval_events.labels(model_id=model_id, action="approved").inc()
    approvals_pending.set(pending_count)


def record_rejected(model_id: str, pending_count: int) -> None:
    approval_events.labels(model_id=model_id, action="rejected").inc()
    approvals_pending.set(pending_count)


def record_prefect_retry(method: str) -> None:
    prefect_retries.labels(method=method).inc()


def record_circuit_breaker_open() -> None:
    circuit_breaker_opens.inc()


def record_retrain(model_name: str, dataset_name: str, outcome: str) -> None:
    retrain_requests.labels(model_name=model_name, dataset_name=dataset_name, outcome=outcome).inc()


def observe_retrain_duration(model_name: str, dataset_name: str, duration_seconds: float) -> None:
    retrain_duration.labels(model_name=model_name, dataset_name=dataset_name).observe(duration_seconds)


def record_approvals_expired(count: int) -> None:
    approvals_expired.inc(count)


def update_age(oldest_pending_ts: str | None) -> None:
    """Set the age gauge from the SQLite-stored naive-UTC ISO timestamp."""
    if oldest_pending_ts is None:
        approval_age_oldest.set(0.0)
        return
    ts = datetime.fromisoformat(oldest_pending_ts).replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    approval_age_oldest.set(max(0.0, age))

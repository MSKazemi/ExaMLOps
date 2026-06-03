# services/control_plane/metrics.py
from __future__ import annotations

from datetime import UTC, datetime

from prometheus_client import Counter, Gauge

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


def record_created(model_id: str, pending_count: int) -> None:
    approval_events.labels(model_id=model_id, action="created").inc()
    approvals_pending.set(pending_count)


def record_approved(model_id: str, pending_count: int) -> None:
    approval_events.labels(model_id=model_id, action="approved").inc()
    approvals_pending.set(pending_count)


def record_rejected(model_id: str, pending_count: int) -> None:
    approval_events.labels(model_id=model_id, action="rejected").inc()
    approvals_pending.set(pending_count)


def update_age(oldest_pending_ts: str | None) -> None:
    """Set the age gauge from the SQLite-stored naive-UTC ISO timestamp."""
    if oldest_pending_ts is None:
        approval_age_oldest.set(0.0)
        return
    ts = datetime.fromisoformat(oldest_pending_ts).replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    approval_age_oldest.set(max(0.0, age))

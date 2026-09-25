# services/control_plane/metrics.py
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

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
    # outcome: success | error | dispatched_unrecorded | dedup | throttled.
    # `dispatched_unrecorded` is deliberately NOT `error`: the training started and only the
    # platform's record of it failed. `HighRetrainErrorRate` counts `error` alone, and retrains are
    # rare enough that one miscounted success is a >20% error rate.
    ["model_name", "dataset_name", "outcome"],
)
retrain_duration: Histogram = Histogram(
    "examlops_retrain_duration_seconds",
    "Wall-clock time to create the Prefect flow run: inside a synchronous POST /retrain, or one "
    "dispatch of a /v1 command by the worker",
    ["model_name", "dataset_name"],
    # Up to 10 min: RetrainDurationP99High fires above 300 s, and histogram_quantile() can never
    # report more than the largest finite bucket — with the old 10 s ceiling it could not fire.
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

# Improvement 16 (round 2): Approval expiry
approvals_expired: Counter = Counter(
    "examlops_approvals_expired_total",
    "Total pending approvals that were auto-expired by the background cleanup",
)

# A scrape that could not read the approval store used to be invisible to Prometheus: the handler
# logged the exception and published the fallback age, and the fallback is 0 — which the gauge
# above documents as "none pending". The one value meaning *all clear* was what a broken store
# produced, so `ApprovalsStale` was guaranteed silent exactly when it mattered. This counter is
# what an alert selects instead.
metrics_scrape_errors: Counter = Counter(
    "examlops_metrics_scrape_errors_total",
    "Scrapes of /metrics whose read of the approval store or the event outbox failed",
)


# Audit writes this process attempted and lost (`examlops.data.audit.audit_best_effort`). Failing
# open is deliberate — a promotion must not be refused because the audit store blinked — but the
# loss must not be silent: those actions happened and are absent from the log, so every coverage
# or completeness statement about that window, the EU AI Act Art. 12 figure included, is unsound
# for it. The counter had no caller anywhere until 2026-09-14: the guides named it as *the* signal
# while nothing in a running deployment could read it.
#
# It is published from the process-local counter on every scrape rather than incremented at the
# drop, so a control plane that restarts does not carry a stale total, and the series exists from
# the first scrape rather than appearing at 1 — an alert selecting a series that does not yet
# exist cannot fire for the outage that creates it.
audit_events_dropped: Counter = Counter(
    "examlops_audit_events_dropped_total",
    "Audit events this process attempted to write and lost, by action",
    ["action"],
)


#: what `publish_dropped_audit_events` has already added to the counter, by action.
_published_drops: dict[str, int] = {}


def publish_dropped_audit_events(counts: dict[str, int]) -> None:
    """Mirror `examlops.data.audit.dropped_audit_events()` onto the scrape.

    The EU AI Act Art. 12 actions are pre-created at zero so an alert selecting them has a series
    to select *before* the first loss; any other action appears when it is first dropped. The
    counter is set rather than incremented, because the source of truth is the audit module's own
    tally for this process.
    """
    from examlops.compliance import ART12_REQUIRED_EVENTS  # noqa: PLC0415

    for action in ART12_REQUIRED_EVENTS:
        counts.setdefault(action, 0)
    for action, n in counts.items():
        # A Counter can only be incremented, so publish the delta since the last scrape and
        # remember what was published. Reading the child's current value would mean reaching
        # into prometheus_client's internals, which is not a public API.
        published = _published_drops.get(action, 0)
        audit_events_dropped.labels(action=action)  # create the series even at zero
        if n > published:
            audit_events_dropped.labels(action=action).inc(n - published)
            _published_drops[action] = n


# Asynchronous commands (plan P1.2): what the worker pool did with each command it picked up, and
# how many are waiting. A queue that only grows is the signal that dispatch is failing or starved.
command_outcomes: Counter = Counter(
    "control_plane_command_outcomes_total",
    "Asynchronous commands handled by the worker pool, by kind and outcome",
    ["kind", "outcome"],  # outcome: succeeded | failed | dead | capacity | busy
)
command_queue_depth: Gauge = Gauge(
    "control_plane_command_queue_depth",
    "Asynchronous commands waiting to be dispatched (pending or retryable-failed)",
)


# Event backbone (plan P2.7). The outbox gauges are read from the store on every scrape, like the
# approval gauges, so a restarted control plane cannot report an empty backlog it has not looked
# at. The consumer and dead-letter gauges come from JetStream and are refreshed by the relay loop
# when the publisher is `nats`; with any other publisher they are absent, not zero.
event_outbox_pending: Gauge = Gauge(
    "examlops_event_outbox_pending", "Outbox events not yet published to the backbone"
)
event_outbox_poison: Gauge = Gauge(
    "examlops_event_outbox_poison",
    "Outbox events that exhausted EXAMLOPS_EVENT_MAX_ATTEMPTS and will not be retried",
)
event_outbox_oldest_age: Gauge = Gauge(
    "examlops_event_outbox_oldest_pending_age_seconds",
    "How long the oldest unpublished outbox event has waited (0 when none is pending)",
)
event_relay_events: Counter = Counter(
    "examlops_event_relay_events_total",
    "Outbox events the control plane's relay handed to the publisher, by outcome",
    # published | failed (the broker refused it) | deferred (the broker was not there at all)
    ["outcome"],
)
event_relay_cycle_errors: Counter = Counter(
    "examlops_event_relay_cycle_errors_total",
    "Relay cycles that raised before publishing anything (publisher misconfigured or unreachable)",
)
event_consumer_pending: Gauge = Gauge(
    "examlops_event_consumer_pending",
    "Events on the backbone a durable consumer has not been delivered yet",
    ["consumer"],
)
event_consumer_ack_pending: Gauge = Gauge(
    "examlops_event_consumer_ack_pending",
    "Events delivered to a durable consumer and not yet acknowledged",
    ["consumer"],
)
event_dlq_messages: Gauge = Gauge(
    "examlops_event_dlq_messages",
    "Events a consumer parked on its dead-letter subject, within the stream's retention",
    ["consumer"],
)


# Serving snapshot (plan P4.2 / ADR 0127). Replicas export the generation they applied as
# `ray_examlops_serving_snapshot_applied_generation`; the difference is serving's config lag.
serving_snapshot_generation: Gauge = Gauge(
    "examlops_serving_snapshot_generation",
    "Newest serving-snapshot generation this control plane published or confirmed",
)
serving_snapshot_last_success: Gauge = Gauge(
    "examlops_serving_snapshot_last_success_timestamp_seconds",
    "Unix time of the last serving-snapshot compile that succeeded",
)
serving_snapshot_errors: Counter = Counter(
    "examlops_serving_snapshot_compile_errors_total",
    "Serving-snapshot compiles that failed (MLflow or the platform database unreadable)",
)


# Drift status changes (plan P2.4b). The error counter is unlabeled, so it exports 0 from the
# start and DriftEvaluationFailing sees its first failure.
drift_evaluation_errors: Counter = Counter(
    "examlops_drift_evaluation_errors_total",
    "Drift evaluations that failed (the platform store or a drift provider unreadable)",
)
drift_status_changes: Counter = Counter(
    "examlops_drift_status_changes_total",
    "Changes of a model's prediction-drift status the control plane announced, by new status",
    ["status"],
)


def record_drift_evaluation_error() -> None:
    drift_evaluation_errors.inc()


def record_drift_status_change(status: str) -> None:
    drift_status_changes.labels(status=status).inc()


# Scheduled audit maintenance (ADR 0028): checkpoint + WORM + transparency log + retention. The
# error counter is unlabeled so it exports 0 from the first scrape; AuditMaintenanceFailing reads
# it. The timestamp is the heartbeat: it moves only on a cycle that finished without a failed step.
audit_maintenance_errors: Counter = Counter(
    "examlops_audit_maintenance_errors_total",
    "Audit maintenance steps that failed (checkpoint signing, WORM anchor, transparency log, prune)",
)
audit_maintenance_last_success: Gauge = Gauge(
    "examlops_audit_maintenance_last_success_timestamp_seconds",
    "Unix time of the last audit maintenance cycle that completed without a failed step",
)


def record_audit_maintenance(result: dict[str, Any], when: float) -> None:
    status = result.get("status")
    if status == "ok":
        audit_maintenance_last_success.set(when)
    elif status == "degraded":
        audit_maintenance_errors.inc(max(1, len(result.get("failed_steps") or [])))
    elif status == "error":
        audit_maintenance_errors.inc()


# Uses of the shared legacy token (plan P3.2). Unlabeled, so it exports 0 from the start: the
# number to watch fall to zero before CONTROL_PLANE_LEGACY_TOKEN=off.
legacy_token_uses: Counter = Counter(
    "control_plane_legacy_token_uses_total",
    "Requests authenticated with the shared legacy CONTROL_PLANE_TOKEN",
)


def record_legacy_token_use() -> None:
    legacy_token_uses.inc()


# Every authenticated request, by principal and how it proved itself: `static` (its own secret),
# `legacy` (the shared token), `workload` (a SPIFFE JWT-SVID, ADR 0125) or `federated` (an IdP
# token; one `federated` principal, since users are unbounded). A service's `static` series that
# stops growing is the signal that its secret can be retired.
authentications: Counter = Counter(
    "control_plane_authentications_total",
    "Authenticated requests by principal and credential kind",
    ["principal", "method"],
)


def initialize_authentications(pairs: Iterable[tuple[str, str]]) -> None:
    """Export 0 for every configured principal, so `increase()` sees its first request."""
    for principal, method in pairs:
        authentications.labels(principal=principal, method=method)


def record_authentication(principal: str, method: str) -> None:
    authentications.labels(principal=principal, method=method).inc()


# ADR 0014 decision 4: project-level authorization at the model routes (`cplane.project_gate`).
# Only three outcomes (bounded cardinality); every label exported at 0 from the start so a rate
# on `deny` or `unavailable` can alert on its first occurrence. `skipped` is not counted: with
# EXAMLOPS_MULTITENANCY off the gate does not decide anything.
PROJECT_AUTHZ_OUTCOMES = ("allow", "deny", "unavailable")
project_authz_decisions: Counter = Counter(
    "control_plane_project_authz_decisions_total",
    "Project-level (ADR 0014) authorization decisions at the control plane's model routes",
    ["outcome"],
)
for _outcome in PROJECT_AUTHZ_OUTCOMES:
    project_authz_decisions.labels(outcome=_outcome)


def record_project_authz(outcome: str) -> None:
    project_authz_decisions.labels(outcome=outcome).inc()


def set_snapshot(generation: int, when: float) -> None:
    serving_snapshot_generation.set(generation)
    serving_snapshot_last_success.set(when)


def record_snapshot_error() -> None:
    serving_snapshot_errors.inc()


def set_outbox(stats: dict[str, int], oldest_age: float | None) -> None:
    event_outbox_pending.set(stats.get("pending", 0))
    event_outbox_poison.set(stats.get("poison", 0))
    event_outbox_oldest_age.set(oldest_age or 0.0)


dead_commands_with_run: Counter = Counter(
    "examlops_control_plane_dead_commands_with_run_total",
    "Commands given up on for which Prefect nevertheless holds a flow run (a dispatch that landed "
    "after the platform stopped waiting)",
)


def record_dead_command_with_run() -> None:
    dead_commands_with_run.inc()


def record_relay(result: dict[str, Any]) -> None:
    event_relay_events.labels(outcome="published").inc(result.get("published", 0))
    event_relay_events.labels(outcome="failed").inc(result.get("failed", 0))
    # Separate from `failed` on purpose: these events were never offered to a broker, so counting
    # them as failures would read as "the backbone rejected 300 events" during a plain outage.
    event_relay_events.labels(outcome="deferred").inc(result.get("deferred", 0))


def record_relay_cycle_error() -> None:
    event_relay_cycle_errors.inc()


# The consumer-lag and dead-letter gauges are refreshed from JetStream by the relay loop, and a
# failed read deliberately keeps their last values — zero lag is the reading that means "healthy",
# and an unreachable broker must not be able to produce it. The cost is that `EventConsumerLagging`
# and `EventDeadLettered` then judge numbers of unknown age. This counter is what says so, exactly
# as `metrics_scrape_errors` does for the approval gauges on the same endpoint. Unlabelled, so the
# series exists from import and an alert can catch the first failure.
event_backbone_read_errors: Counter = Counter(
    "examlops_event_backbone_read_errors_total",
    "Reads of JetStream consumer/dead-letter statistics that failed, leaving the gauges stale",
)


def record_backbone_read_error() -> None:
    event_backbone_read_errors.inc()


def set_backbone(stats: dict[str, dict]) -> None:
    """Replace the consumer and dead-letter gauges with JetStream's current view.

    Cleared first, so a consumer deleted from the stream stops being reported rather than
    freezing at its last lag forever.
    """
    event_consumer_pending.clear()
    event_consumer_ack_pending.clear()
    event_dlq_messages.clear()
    for name, c in stats.get("consumers", {}).items():
        event_consumer_pending.labels(consumer=name).set(c.get("pending", 0))
        event_consumer_ack_pending.labels(consumer=name).set(c.get("ack_pending", 0))
    for name, count in stats.get("dlq", {}).items():
        event_dlq_messages.labels(consumer=name).set(count)


COMMAND_OUTCOMES = ("succeeded", "failed", "dead", "capacity", "busy")


def initialize_command_outcomes(kinds: Iterable[str]) -> None:
    """Export every (kind, outcome) series at 0 before the first command runs.

    A labeled series appears when it is first incremented, so the first dead command would arrive
    as a new series already at 1, and ``increase()`` over a series with no earlier sample is 0:
    ControlPlaneCommandDead would miss exactly the rare event it exists for.
    """
    for kind in kinds:
        for outcome in COMMAND_OUTCOMES:
            command_outcomes.labels(kind=kind, outcome=outcome)


def record_command_outcome(kind: str, outcome: str) -> None:
    command_outcomes.labels(kind=kind, outcome=outcome).inc()


def set_command_queue_depth(depth: int) -> None:
    command_queue_depth.set(depth)


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
    retrain_duration.labels(model_name=model_name, dataset_name=dataset_name).observe(
        duration_seconds
    )


def record_approvals_expired(count: int) -> None:
    approvals_expired.inc(count)


def record_scrape_error() -> None:
    """A /metrics scrape could not read the approval store.

    The gauges are deliberately left alone. Publishing a fallback would overwrite the last values
    known to be true with ones that mean "queue empty, nothing waiting", and neither approval alert
    could fire for as long as the store stayed broken. A stale gauge is honest about being stale;
    a fabricated zero is not.
    """
    metrics_scrape_errors.inc()


def set_pending(pending_count: int) -> None:
    """Set the queue depth from the store rather than from this process's event history.

    ``record_created``/``record_approved``/``record_rejected`` only move the gauge when an approval
    event happens *in this process*. Restart the control plane with a full queue and nobody
    approving, and the gauge reads 0 until the next event — the reading that means "nothing is
    waiting" published at the moment a backlog is going unattended.
    """
    approvals_pending.set(pending_count)


def update_age(oldest_pending_ts: str | None) -> None:
    """Set the age gauge from the SQLite-stored naive-UTC ISO timestamp."""
    if oldest_pending_ts is None:
        approval_age_oldest.set(0.0)
        return
    ts = datetime.fromisoformat(oldest_pending_ts).replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    approval_age_oldest.set(max(0.0, age))


# ADR 0017 clause 4 — feature-store freshness monitoring. Published from the registry on every
# scrape (never carried in memory), one series per view, so a restart cannot hide a stale view.
# `age` is absent for a never-materialized view (there is no age to report), and `stale` is 1 for
# it: a view nobody has materialized is serving nothing, which is the state the alert exists for.
feature_view_age: Gauge = Gauge(
    "examlops_feature_view_age_seconds",
    "Seconds since the feature view was last materialized to the online store",
    ["view"],
)
feature_view_stale: Gauge = Gauge(
    "examlops_feature_view_stale",
    "1 when the feature view is past its TTL or was never materialized, else 0",
    ["view"],
)
feature_view_ttl: Gauge = Gauge(
    "examlops_feature_view_ttl_seconds",
    "Declared freshness TTL of the feature view (0 = no staleness alert)",
    ["view"],
)
feature_materializations: Counter = Counter(
    "examlops_feature_materializations_total",
    "Scheduled feature-view materializations by outcome "
    "(materialized | failed | skipped | mirror_failed)",
    ["outcome"],
)
for _outcome in ("materialized", "failed", "skipped", "mirror_failed"):
    feature_materializations.labels(outcome=_outcome)
# A failed freshness read keeps the gauges at their last values (a fabricated 0 would read as
# "fresh"), and a process that has never read them publishes no series at all, so FeatureViewStale
# cannot fire. Its own counter, not the approval store's scrape-error counter: that one's alert
# tells the operator the approval store is unreadable. Unlabelled, so it exists from import.
feature_freshness_read_errors: Counter = Counter(
    "examlops_feature_freshness_read_errors_total",
    "Scrapes whose read of feature-view freshness failed, leaving the freshness gauges stale",
)


def set_feature_freshness(rows: Iterable[Any]) -> None:
    """Publish ``examlops.feature_store.scheduler.freshness_report()`` onto the scrape.

    Views that disappeared from the registry are removed, so a deleted view cannot keep a stale
    series alive and page on something that no longer exists.
    """
    seen: set[str] = set()
    for row in rows:
        seen.add(row.view)
        if row.age_seconds is not None:
            feature_view_age.labels(view=row.view).set(float(row.age_seconds))
        # A view with neither a TTL nor a schedule has no freshness promise to break, so it is
        # never reported stale — otherwise a hand-applied, never-materialized view would page.
        promised = row.ttl_seconds > 0 or row.interval_seconds > 0
        feature_view_stale.labels(view=row.view).set(1.0 if row.stale and promised else 0.0)
        feature_view_ttl.labels(view=row.view).set(float(row.ttl_seconds))
    for gauge in (feature_view_age, feature_view_stale, feature_view_ttl):
        for labels in list(gauge._metrics):  # noqa: SLF001 - prometheus_client has no public list
            if labels[0] not in seen:
                gauge.remove(*labels)


def record_feature_materialization_cycle(result: dict[str, Any]) -> None:
    # `mirror_failed` also counts under `materialized`: the durable write happened, the serving
    # tier (Redis) did not take it.
    for outcome in ("materialized", "failed", "skipped", "mirror_failed"):
        count = len(result.get(outcome) or [])
        if count:
            feature_materializations.labels(outcome=outcome).inc(count)


def record_feature_freshness_read_error() -> None:
    feature_freshness_read_errors.inc()

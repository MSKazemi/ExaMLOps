"""Prometheus hooks for the dataplane stream ingress (ADR 0130/0131, Plan 2, task A5).

Same shape as :mod:`examlops.dataplane.metrics`: a lazily built registry, and a no-op when
``prometheus-client`` is not installed — every function here is safe to call unconditionally from
the request path. Names are ``dataplane_stream_*`` (Plan 2 global constraint); the embedding
gauges are defined here and nowhere else (A3 finding I5), and the DB telemetry sink reaches the
baseline gauges only through :func:`on_baseline`.

Label rules (controller rulings R7 and R9.5):

* Every per-stream series carries ``project`` as well as ``stream`` — two projects may reuse a
  stream name, and their series must not merge.
* ``dataplane_stream_telemetry_dropped_total{project,stream}`` — counted by the ingress when
  ``TelemetrySpool.offer()`` refuses a record (the stream is known there). The spool's own
  ``on_drop`` hook (a drop at close/timeout, where no stream is known) counts under
  ``project="_spool", stream="_spool"`` — see :func:`spool_hooks`.
* ``dataplane_stream_telemetry_failed_total`` — **no labels**: fed by the spool's ``on_fail`` hook,
  and a sink failure is process-wide (the database is down), not a property of one stream.
* ``dataplane_stream_consumer_lag{project,stream,partition}`` — the Kafka connector's distance
  from each owned partition's high watermark, and cleared when the partition is revoked. The
  watermark is librdkafka's cached one, refreshed by a real query on a bounded schedule
  (``kafka_stream.WATERMARK_REFRESH_INTERVAL_S``): the cache is fed by fetch responses only, so a
  paused or retry-parked partition would otherwise report no lag exactly while its backlog grows.
* ``dataplane_stream_connector_state{project,stream,state}`` — one-hot, and cleared outright by
  :func:`clear_connector_state` when the supervisor stops supervising the stream (it was deleted,
  or became a push stream): a series nothing owns any more reports nothing, rather than reporting
  ``stopped`` for ever.
* ``dataplane_stream_embedding_{norm,mean,std}{model}`` — set by the ingress from the request's
  embedding summary; ``…_baseline{model}`` — set by :func:`on_baseline` from the stats keys
  ``norm_mean``/``mean_mean``/``std_mean``, exactly as the SeanerBUS bridge reads them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

#: The ``project``/``stream`` label value for drops the spool counts itself (close/timeout — no
#: stream known).
SPOOL_STREAM_LABEL = "_spool"

_METRICS: dict[str, Any] | None = None
_METRICS_LOCK = threading.Lock()

# The last state each (project, stream)'s one-hot ``connector_state`` gauge was set to, so a state
# change zeroes the previous series instead of leaving two states at 1, and every state it has ever
# published, so :func:`clear_connector_state` can take the whole stream off the gauge — the zeroed
# ones included — when the stream stops being supervised.
_CONNECTOR_STATES: dict[tuple[str, str], str] = {}
_CONNECTOR_SERIES: dict[tuple[str, str], set[str]] = {}
_STATES_LOCK = threading.Lock()


def _metrics() -> dict[str, Any]:
    global _METRICS
    if _METRICS is not None:
        return _METRICS
    with _METRICS_LOCK:
        if _METRICS is not None:  # built by another thread while we waited
            return _METRICS
        try:
            from prometheus_client import Counter, Gauge, Histogram
        except Exception:
            _METRICS = {}
            return _METRICS
        metrics: dict[str, Any] = {
            "requests": Counter(
                "dataplane_stream_requests_total",
                "Stream ingress requests by outcome",
                ["project", "stream", "connector", "model", "outcome"],
            ),
            "duration": Histogram(
                "dataplane_stream_request_duration_seconds",
                "Stream ingress request duration, admission to reply",
                ["project", "stream", "connector"],
                buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            ),
            "in_flight": Gauge(
                "dataplane_stream_in_flight",
                "Stream requests currently holding an in-flight permit",
                ["project", "stream"],
            ),
            "shed": Counter(
                "dataplane_stream_shed_total",
                "Stream requests refused by admission control, by reason (in_flight | rate)",
                ["project", "stream", "reason"],
            ),
            "telemetry_dropped": Counter(
                "dataplane_stream_telemetry_dropped_total",
                "Telemetry records refused by the spool (full or closed)",
                ["project", "stream"],
            ),
            "telemetry_failed": Counter(
                "dataplane_stream_telemetry_failed_total",
                "Telemetry records the sink failed to persist",
            ),
            "connector_state": Gauge(
                "dataplane_stream_connector_state",
                "Stream connector state (1 = the stream is in this state)",
                ["project", "stream", "state"],
            ),
            "messages_expired": Counter(
                "dataplane_stream_messages_expired_total",
                "Messages that left the log (retention, compaction, DeleteRecords) while parked "
                "for a retry; each is dead-lettered as expired_from_log",
                ["project", "stream"],
            ),
            "consumer_lag": Gauge(
                "dataplane_stream_consumer_lag",
                "Messages between this consumer's stored position and the partition's high "
                "watermark (Kafka connector; the broker's cached watermark, no extra round trip)",
                ["project", "stream", "partition"],
            ),
            "dead_letters": Counter(
                "dataplane_stream_dead_letters_total",
                "Dead letters recorded in the database (a redelivered one is not counted again), "
                "by reason; a reason outside the known set is counted as 'other'",
                ["project", "stream", "reason"],
            ),
        }
        for stat in ("norm", "mean", "std"):
            metrics[f"emb_{stat}"] = Gauge(
                f"dataplane_stream_embedding_{stat}",
                f"Most recent request embedding {stat}, per model",
                ["model"],
            )
            metrics[f"emb_{stat}_baseline"] = Gauge(
                f"dataplane_stream_embedding_{stat}_baseline",
                f"Recorded baseline embedding {stat}, per model (exa drift input baseline)",
                ["model"],
            )
        _METRICS = metrics
    return _METRICS


def observe_request(
    project: str, stream: str, connector: str, model: str, outcome: str, seconds: float
) -> None:
    """One request's RED sample: its outcome and how long it took up to the reply."""
    m = _metrics()
    if not m:
        return
    m["requests"].labels(
        project=project, stream=stream, connector=connector, model=model, outcome=outcome
    ).inc()
    m["duration"].labels(project=project, stream=stream, connector=connector).observe(
        max(0.0, seconds)
    )


def in_flight_inc(project: str, stream: str) -> None:
    m = _metrics()
    if m:
        m["in_flight"].labels(project=project, stream=stream).inc()


def in_flight_dec(project: str, stream: str) -> None:
    m = _metrics()
    if m:
        m["in_flight"].labels(project=project, stream=stream).dec()


def shed(project: str, stream: str, reason: str) -> None:
    """A request refused by admission control; ``reason`` is ``in_flight`` or ``rate``."""
    m = _metrics()
    if m:
        m["shed"].labels(project=project, stream=stream, reason=reason).inc()


def telemetry_dropped(project: str, stream: str) -> None:
    m = _metrics()
    if m:
        m["telemetry_dropped"].labels(project=project, stream=stream).inc()


def messages_expired(project: str, stream: str, count: int = 1) -> None:
    """``count`` parked messages found gone from the log (Kafka stream connector, A7)."""
    m = _metrics()
    if m and count > 0:
        m["messages_expired"].labels(project=project, stream=stream).inc(count)


def set_consumer_lag(project: str, stream: str, partition: int, lag: int) -> None:
    """One partition's consumer lag (ADR 0131 d11; review M14 — the ADR named the series and
    nothing emitted it, so batch S3's ``DataplaneStreamConsumerLagHigh`` alert would have had
    nothing to fire on). The label set is bounded by the stream's partition count."""
    m = _metrics()
    if m:
        m["consumer_lag"].labels(project=project, stream=stream, partition=str(partition)).set(
            max(0, lag)
        )


def clear_consumer_lag(project: str, stream: str, partition: int) -> None:
    """Forget one partition's lag series — a partition this consumer no longer owns would
    otherwise report its last value for ever, and alert on a backlog somebody else is serving."""
    m = _metrics()
    if not m:
        return
    try:
        m["consumer_lag"].remove(project, stream, str(partition))
    except KeyError:  # never published for this partition
        pass


#: The ``reason`` label values of ``dataplane_stream_dead_letters_total``: the dead-letter reasons
#: (``examlops.dataplane.streams.dlq.REASON_*``, the Kafka connector's ``expired_from_log``) and the
#: ingress outcomes that dead-letter at once. Anything else is ``other``, so the label stays bounded.
DEAD_LETTER_REASONS = frozenset(
    {
        "validation",
        "not_found",
        "unexpected",
        "oversize",
        "not_json",
        "invalid_message",
        "retries_exhausted",
        "expired_from_log",
    }
)
DEAD_LETTER_OTHER_REASON = "other"


def dead_letter_reason(reason: str) -> str:
    """``reason`` if it is one of :data:`DEAD_LETTER_REASONS`, else ``other``."""
    return reason if reason in DEAD_LETTER_REASONS else DEAD_LETTER_OTHER_REASON


def dead_letter(project: str, stream: str, reason: str) -> None:
    """One new dead-letter row (task A7b); ``reason`` is bounded by :func:`dead_letter_reason`."""
    m = _metrics()
    if m:
        m["dead_letters"].labels(
            project=project, stream=stream, reason=dead_letter_reason(reason)
        ).inc()


def telemetry_failed() -> None:
    m = _metrics()
    if m:
        m["telemetry_failed"].inc()


def set_embedding(model: str, norm: float, mean: float, std: float) -> None:
    """The latest request embedding summary for ``model`` (never the raw vector)."""
    m = _metrics()
    if not m:
        return
    m["emb_norm"].labels(model=model).set(norm)
    m["emb_mean"].labels(model=model).set(mean)
    m["emb_std"].labels(model=model).set(std)


def on_baseline(model: str, stats: dict[str, Any]) -> None:
    """``DbTelemetrySink(on_baseline=…)`` callback: publish the recorded input baseline.

    Reads the keys the bridge reads (``norm_mean``/``mean_mean``/``std_mean``); a missing or
    non-numeric key leaves its gauge unset rather than publishing a zero, which would draw a floor
    on the panel and read as a real measurement. Never raises.
    """
    m = _metrics()
    if not m:
        return
    for key, name in (("norm_mean", "norm"), ("mean_mean", "mean"), ("std_mean", "std")):
        value = stats.get(key)
        if value is None:
            continue
        try:
            m[f"emb_{name}_baseline"].labels(model=model).set(float(value))
        except (TypeError, ValueError):
            continue


def set_connector_state(project: str, stream: str, state: str) -> None:
    """Mark the stream's connector as being in ``state`` (1), zeroing its previous state.

    The bookkeeping and both gauge writes happen under one lock, so two concurrent writers for
    one stream can never leave two states at 1.
    """
    m = _metrics()
    if not m:
        return
    gauge = m["connector_state"]
    with _STATES_LOCK:
        previous = _CONNECTOR_STATES.get((project, stream))
        _CONNECTOR_STATES[(project, stream)] = state
        _CONNECTOR_SERIES.setdefault((project, stream), set()).add(state)
        if previous is not None and previous != state:
            gauge.labels(project=project, stream=stream, state=previous).set(0)
        gauge.labels(project=project, stream=stream, state=state).set(1)


def clear_connector_state(project: str, stream: str) -> None:
    """Forget a stream's ``connector_state`` series entirely — every state it ever published, not
    only the one at 1.

    A stream deleted from the catalog, or one this process no longer supervises, otherwise keeps
    reporting ``state="stopped" 1`` until the process restarts (live finding D8): a deleted stream
    goes on looking like a stream that is down, and a dashboard counting stopped streams counts
    one that no longer exists. The lag series already behave this way on revoke
    (:func:`clear_consumer_lag`); this is the same rule for the state gauge.
    """
    m = _metrics()
    with _STATES_LOCK:
        _CONNECTOR_STATES.pop((project, stream), None)
        states = _CONNECTOR_SERIES.pop((project, stream), set())
    if not m:
        return
    gauge = m["connector_state"]
    for state in states:
        try:
            gauge.remove(project, stream, state)
        except KeyError:  # never published (another registry, or already removed)
            pass


def spool_hooks() -> tuple[Callable[[], None], Callable[[], None]]:
    """``(on_drop, on_fail)`` for ``TelemetrySpool(sink, on_drop=…, on_fail=…)``.

    ``on_drop`` counts under ``project="_spool", stream="_spool"``; ``on_fail`` feeds the
    unlabelled failure counter.
    """

    def _on_drop() -> None:
        telemetry_dropped(SPOOL_STREAM_LABEL, SPOOL_STREAM_LABEL)

    return _on_drop, telemetry_failed


__all__ = [
    "DEAD_LETTER_OTHER_REASON",
    "DEAD_LETTER_REASONS",
    "SPOOL_STREAM_LABEL",
    "clear_connector_state",
    "clear_consumer_lag",
    "dead_letter",
    "dead_letter_reason",
    "in_flight_dec",
    "in_flight_inc",
    "messages_expired",
    "observe_request",
    "on_baseline",
    "set_connector_state",
    "set_consumer_lag",
    "set_embedding",
    "shed",
    "spool_hooks",
    "telemetry_dropped",
    "telemetry_failed",
]

"""OpenTelemetry tracing bootstrap for ExaMLOps services that cannot use the
``opentelemetry-instrument`` launcher (e.g. Ray Serve replicas).

Gated by the standard ``OTEL_SDK_DISABLED`` env: when truthy or unset, every
call is a no-op so local dev, unit tests and the non-monitoring stack are
unaffected.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("examlops.observability")

_TRUTHY = {"1", "true", "yes", "on"}


def tracing_enabled() -> bool:
    # Default OFF when unset, so importing this never turns tracing on by surprise.
    return os.getenv("OTEL_SDK_DISABLED", "true").strip().lower() not in _TRUTHY


def _build_sampler():  # type: ignore[no-untyped-def]
    """Build a trace sampler from the standard OTel env vars (Phase 0 item 0.2 / QW4).

    A manually-built ``TracerProvider`` otherwise samples **every** trace (``ParentBased(ALWAYS_ON)``),
    so simply enabling tracing across the fleet firehoses Tempo. Default here to
    ``parentbased_traceidratio`` at 5% — children follow the root's decision, and only ~1 in 20 root
    traces is recorded — while still honoring ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` so
    operators can dial it up (e.g. to 1.0 for a debugging window) or switch strategies.
    """
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    name = os.getenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio").strip().lower()
    try:
        ratio = float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "0.05"))
    except ValueError:
        ratio = 0.05
    ratio = min(max(ratio, 0.0), 1.0)
    return {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
        "traceidratio": TraceIdRatioBased(ratio),
        "parentbased_always_on": ParentBased(ALWAYS_ON),
        "parentbased_always_off": ParentBased(ALWAYS_OFF),
        "parentbased_traceidratio": ParentBased(TraceIdRatioBased(ratio)),
    }.get(name, ParentBased(TraceIdRatioBased(ratio)))


def setup_tracing(service_name: str) -> bool:
    """Configure a global OTLP tracer provider. Returns True if configured,
    False if tracing is disabled. Safe to call more than once."""
    if not tracing_enabled():
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return True  # already configured

    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})
    provider = TracerProvider(resource=resource, sampler=_build_sampler())
    if otlp_protocol() == "http/protobuf":
        from examlops.telemetry.otlp_http import build_http_exporter, parse_headers

        primary = build_http_exporter(
            _http_traces_endpoint(), headers=parse_headers(_otlp_headers()), name="otlp"
        )
    else:
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
        primary = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(primary))
    # Agent-observability consumers (ADR 0021 decision 2): Langfuse / Phoenix each get their own
    # processor *beside* the primary exporter — OTel stays the source of truth, and a consumer
    # that is down or misconfigured never costs the primary pipeline a span.
    try:
        from examlops.telemetry.otlp_http import consumer_exporters

        for _name, exporter in consumer_exporters():
            provider.add_span_processor(BatchSpanProcessor(exporter))
    except Exception as exc:  # noqa: BLE001 - a consumer must never stop a service starting
        _log.warning("agent-observability consumers not configured: %s", exc)
    trace.set_tracer_provider(provider)
    return True


def otlp_protocol() -> str:
    """The primary exporter's protocol: ``grpc`` (default) or ``http/protobuf``.

    Read from the standard ``OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`` / ``OTEL_EXPORTER_OTLP_PROTOCOL``.
    ``http/json`` is not supported and falls back to gRPC with a warning — the historic default,
    rather than a service that silently exports nothing.
    """
    raw = (
        (
            os.getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
            or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
            or "grpc"
        )
        .strip()
        .lower()
    )
    if raw in ("grpc", "http/protobuf"):
        return raw
    _log.warning("OTLP protocol %r is not supported; exporting over grpc", raw)
    return "grpc"


def _otlp_headers() -> str:
    return (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
        or os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
        or ""
    )


def _http_traces_endpoint() -> str:
    """Per the OTel spec: the signal-specific variable is used as-is, the base one gets a path."""
    specific = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if specific:
        return specific
    from examlops.telemetry.otlp_http import traces_endpoint

    return traces_endpoint(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318"))

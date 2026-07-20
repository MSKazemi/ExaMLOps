"""OpenTelemetry tracing bootstrap for ExaMLOps services that cannot use the
``opentelemetry-instrument`` launcher (e.g. Ray Serve replicas).

Gated by the standard ``OTEL_SDK_DISABLED`` env: when truthy or unset, every
call is a no-op so local dev, unit tests and the non-monitoring stack are
unaffected.
"""

from __future__ import annotations

import os

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
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return True

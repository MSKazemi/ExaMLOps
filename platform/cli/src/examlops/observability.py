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
    provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return True

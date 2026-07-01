import importlib

from examlops import observability


def test_setup_tracing_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert observability.tracing_enabled() is False
    assert observability.setup_tracing("test-svc") is False


def test_setup_tracing_enabled_configures_provider(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    importlib.reload(observability)
    assert observability.tracing_enabled() is True
    assert observability.setup_tracing("test-svc") is True

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    assert isinstance(trace.get_tracer_provider(), TracerProvider)

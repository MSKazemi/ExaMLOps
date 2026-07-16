# tests/unit/test_genai_telemetry.py
"""C1 — OpenTelemetry GenAI semantic-convention spans (ADR 0006, spec C1).

Covers GWT-1 (span + tokens), GWT-2 (privacy default), GWT-3 (cost),
GWT-4 (tool failure span), GWT-5 (disabled → no-op).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.telemetry import genai  # noqa: E402


@pytest.fixture
def spans(monkeypatch):
    """Enable tracing with an in-memory exporter; yields the finished-span list."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Force our provider (override any global set by earlier tests).
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    yield exporter
    exporter.clear()


def _attrs(exporter):
    return {s.name: dict(s.attributes) for s in exporter.get_finished_spans()}


# --- cost core (pure) --------------------------------------------------------


def test_estimate_cost_deterministic():
    c1 = genai.estimate_cost("gpt-4o", 1000, 1000)
    c2 = genai.estimate_cost("gpt-4o", 1000, 1000)
    assert c1 == c2
    assert c1 == round(0.0025 + 0.01, 6)


def test_estimate_cost_self_hosted_zero():
    assert genai.estimate_cost("llama3.1:8b", 5000, 5000) == 0.0


def test_estimate_cost_unknown_model_uses_default():
    assert genai.estimate_cost("mystery-model", 1000, 0) == round(genai._DEFAULT_RATE_IN, 6)


# --- GWT-5: disabled → no-op -------------------------------------------------


def test_gwt5_disabled_noop(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    with genai.genai_span("model", system="openai", model="gpt-4o") as span:
        assert getattr(span, "is_noop", False) is True
        # methods must not raise
        cost = genai.record_usage(span, model="gpt-4o", input_tokens=10, output_tokens=5)
        assert cost == genai.estimate_cost("gpt-4o", 10, 5)


def test_gwt5_unset_is_disabled(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    assert genai.tracing_enabled() is False


# --- GWT-1: span + tokens ----------------------------------------------------


def test_gwt1_model_span_has_semconv_attrs(spans):
    with genai.genai_span(
        "model", system="openai", model="gpt-4o", tenant="acme", request_hash="abc123"
    ) as span:
        genai.record_usage(
            span, model="gpt-4o", input_tokens=120, output_tokens=42, finish_reasons=["stop"]
        )
    a = _attrs(spans)
    key = "gen_ai.model gpt-4o"
    assert key in a
    attrs = a[key]
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 42
    assert attrs["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs["examlops.tenant"] == "acme"
    assert attrs["examlops.request_hash"] == "abc123"
    assert attrs["examlops.semconv.version"] == genai.SEMCONV_VERSION


def test_extras_alias_version(spans):
    with genai.genai_span(
        "model", system="mlflow", model="jpcp", alias="Production", version="17"
    ) as span:
        genai.record_usage(span, model="jpcp", input_tokens=1, output_tokens=1)
    attrs = next(iter(_attrs(spans).values()))
    assert attrs["examlops.model.alias"] == "Production"
    assert attrs["examlops.model.version"] == "17"


# --- GWT-3: cost -------------------------------------------------------------


def test_gwt3_cost_attribute(spans):
    with genai.genai_span("model", system="openai", model="gpt-4o") as span:
        cost = genai.record_usage(span, model="gpt-4o", input_tokens=1000, output_tokens=1000)
    attrs = next(iter(_attrs(spans).values()))
    assert attrs["examlops.cost.usd"] == cost == round(0.0025 + 0.01, 6)


# --- GWT-2: privacy ----------------------------------------------------------


def test_gwt2_no_content_by_default(spans, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", raising=False)
    with genai.genai_span("model", system="openai", model="gpt-4o") as span:
        captured = genai.maybe_capture_content(span, prompt="secret", completion="answer")
    assert captured is False
    attrs = next(iter(_attrs(spans).values()))
    assert "gen_ai.prompt" not in attrs
    assert "gen_ai.completion" not in attrs


def test_content_captured_and_redacted_when_enabled(spans, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", "true")
    genai.set_redactor(lambda t: t.replace("secret", "[REDACTED]"))
    try:
        with genai.genai_span("model", system="openai", model="gpt-4o") as span:
            captured = genai.maybe_capture_content(span, prompt="my secret", completion="ok")
        assert captured is True
        attrs = next(iter(_attrs(spans).values()))
        assert attrs["gen_ai.prompt"] == "my [REDACTED]"
        assert attrs["gen_ai.completion"] == "ok"
    finally:
        genai.set_redactor(lambda t: t)


# --- GWT-4: tool failure span ------------------------------------------------


def test_gwt4_tool_failure_span(spans):
    from opentelemetry.trace import Status, StatusCode

    with genai.genai_span("tool", system="skipper", model="get_status", tenant="acme") as span:
        span.set_attribute("gen_ai.tool.name", "get_status")
        span.set_status(Status(StatusCode.ERROR, "boom"))
        span.set_attribute("examlops.tool.success", False)
    finished = spans.get_finished_spans()
    tool = next(s for s in finished if s.attributes.get("gen_ai.operation.name") == "tool")
    assert tool.attributes["examlops.tool.success"] is False
    assert tool.status.status_code.name == "ERROR"

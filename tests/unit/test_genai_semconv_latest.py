# tests/unit/test_genai_semconv_latest.py
"""USAR I0 — the opt-in emits the current GenAI conventions, and only them (ADR 0148 d2, ADR 0115).

Under ``gen_ai_latest_experimental`` the instrumentation used to change only the shape of captured
content while still emitting ``gen_ai.system`` and the 2024 operation names ``model``/``agent``/
``tool``. OpenTelemetry defines that token as a wholesale switch to the latest conventions, so the
opt-in now emits ``gen_ai.provider.name`` and the registry's operation names — checked here against
the GenAI registry at the pinned ``semantic-conventions-genai`` commit. Without the opt-in nothing
changes: an instrumentation keeps emitting what it already emitted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.telemetry import genai  # noqa: E402

# `gen_ai.operation.name` members of model/gen-ai/registry.yaml in
# open-telemetry/semantic-conventions-genai @ 0c8759497519 (2026-09-10). Re-pin together with
# genai.LATEST_REVISION — the test below fails if the two drift apart.
REGISTRY_REVISION = "0c8759497519"
REGISTRY_OPERATIONS = frozenset(
    {
        "chat",
        "generate_content",
        "text_completion",
        "embeddings",
        "retrieval",
        "fetch_response",
        "create_agent",
        "invoke_agent",
        "execute_tool",
        "invoke_workflow",
        "plan",
        "search_memory",
        "create_memory",
        "update_memory",
        "upsert_memory",
        "delete_memory",
        "create_memory_store",
        "delete_memory_store",
    }
)


@pytest.fixture
def spans(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **k: provider.get_tracer("t"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    return exporter


def test_the_vendored_registry_list_is_the_pinned_revision():
    assert genai.LATEST_REVISION == REGISTRY_REVISION


def test_every_opt_in_operation_name_is_in_the_registry(monkeypatch):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    for kind in sorted(genai._SPAN_KINDS):
        assert genai.operation_name(kind) in REGISTRY_OPERATIONS, kind


@pytest.mark.parametrize(
    ("kind", "operation"),
    [
        ("model", "text_completion"),
        ("agent", "invoke_agent"),
        ("tool", "execute_tool"),
        ("chat", "chat"),
    ],
)
def test_the_opt_in_switches_names_wholesale(monkeypatch, spans, kind, operation):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    with genai.genai_span(kind, system="vllm-server", model="qwen"):
        pass
    (span,) = spans.get_finished_spans()
    assert span.name == f"{operation} qwen"
    assert span.attributes["gen_ai.operation.name"] == operation
    assert span.attributes["gen_ai.provider.name"] == "vllm-server"
    assert "gen_ai.system" not in span.attributes


def test_without_the_opt_in_the_pinned_shape_is_unchanged(monkeypatch, spans):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    with genai.genai_span("model", system="vllm-server", model="qwen"):
        pass
    (span,) = spans.get_finished_spans()
    assert span.name == "gen_ai.model qwen"
    assert span.attributes["gen_ai.operation.name"] == "model"
    assert span.attributes["gen_ai.system"] == "vllm-server"
    assert "gen_ai.provider.name" not in span.attributes
    assert span.attributes["examlops.semconv.version"] == genai.SEMCONV_VERSION


def test_callback_spans_follow_the_same_switch(monkeypatch, spans):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    span = genai.start_span("tool", system="skipper", model="search")
    span.end()
    (finished,) = spans.get_finished_spans()
    assert finished.name == "execute_tool search"
    assert finished.attributes["gen_ai.provider.name"] == "skipper"

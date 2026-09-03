"""ADR 0006 clause 2, third boundary — Skipper's own LLM and tool calls.

The clause names the gateway, the serving path and Skipper. The first two were instrumented in
the platform package; this covers the third, which was the ADR's last open clause.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT / "platform" / "services" / "agent"))
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

from skipper import genai_trace  # noqa: E402


@pytest.fixture
def spans(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    yield exporter
    exporter.clear()


class _Msg:
    def __init__(self, usage=None):
        self.usage_metadata = usage


class _Gen:
    def __init__(self, usage=None, finish=None):
        self.message = _Msg(usage)
        self.generation_info = {"finish_reason": finish} if finish else {}


class _Result:
    def __init__(self, generations, llm_output=None):
        self.generations = generations
        self.llm_output = llm_output


# ── attaching ─────────────────────────────────────────────────────────────────


def test_no_handler_and_an_untouched_config_when_tracing_is_off(monkeypatch):
    """The default path must be byte-identical, not 'the same plus an inert handler'."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    cfg = {"configurable": {"thread_id": "t"}}
    assert genai_trace.tracer() is None
    assert genai_trace.traced(cfg) == cfg
    assert "callbacks" not in genai_trace.traced(cfg)


def test_the_handler_is_attached_when_tracing_is_on(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    out = genai_trace.traced({"configurable": {"thread_id": "t"}})
    assert isinstance(out["callbacks"][0], genai_trace.SkipperTracer)
    assert out["configurable"] == {"thread_id": "t"}


def test_existing_callbacks_are_preserved(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    mine = object()
    out = genai_trace.traced({"callbacks": [mine]})
    assert out["callbacks"][0] is mine
    assert len(out["callbacks"]) == 2


def test_traced_does_not_mutate_the_caller_s_config(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    cfg = {"configurable": {"thread_id": "t"}}
    genai_trace.traced(cfg)
    assert cfg == {"configurable": {"thread_id": "t"}}


# ── model spans ───────────────────────────────────────────────────────────────


def test_a_chat_model_call_emits_a_chat_span_with_usage(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_chat_model_start({"name": "ChatOllama"}, [], run_id="r1", metadata={})
    tracer.on_llm_end(
        _Result([[_Gen(usage={"input_tokens": 12, "output_tokens": 7}, finish="stop")]]),
        run_id="r1",
    )
    (span,) = spans.get_finished_spans()
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.system"] == "skipper"
    assert span.attributes["gen_ai.usage.input_tokens"] == 12
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert span.attributes["gen_ai.response.finish_reasons"] == ("stop",)


def test_the_model_name_comes_from_langchain_metadata_when_present(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_chat_model_start(
        {"name": "ChatOllama"}, [], run_id="r", metadata={"ls_model_name": "llama3.1:8b"}
    )
    tracer.on_llm_end(_Result([[]]), run_id="r")
    assert spans.get_finished_spans()[0].attributes["gen_ai.request.model"] == "llama3.1:8b"


def test_provider_raw_token_usage_is_read_when_the_normalized_form_is_absent(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_chat_model_start({"name": "m"}, [], run_id="r")
    tracer.on_llm_end(
        _Result(
            [[_Gen()]], llm_output={"token_usage": {"prompt_tokens": 3, "completion_tokens": 4}}
        ),
        run_id="r",
    )
    attrs = spans.get_finished_spans()[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["gen_ai.usage.output_tokens"] == 4


def test_unreported_usage_is_not_recorded_as_zero(spans):
    """A zero token count and an unreported one are different facts."""
    tracer = genai_trace.SkipperTracer()
    tracer.on_chat_model_start({"name": "m"}, [], run_id="r")
    tracer.on_llm_end(_Result([[_Gen()]]), run_id="r")
    attrs = spans.get_finished_spans()[0].attributes
    assert "gen_ai.usage.input_tokens" not in attrs
    assert "gen_ai.usage.output_tokens" not in attrs


def test_a_model_error_is_recorded_and_the_span_still_ends(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_chat_model_start({"name": "m"}, [], run_id="r")
    tracer.on_llm_error(RuntimeError("backend down"), run_id="r")
    (span,) = spans.get_finished_spans()
    assert span.attributes["error.type"] == "RuntimeError"
    assert span.events


# ── tool spans ────────────────────────────────────────────────────────────────


def test_a_tool_call_emits_a_tool_span_named_for_the_tool(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_tool_start({"name": "get_status"}, "{}", run_id="t1")
    tracer.on_tool_end("ok", run_id="t1")
    (span,) = spans.get_finished_spans()
    assert span.attributes["gen_ai.operation.name"] == "tool"
    assert span.attributes["gen_ai.request.model"] == "get_status"


def test_a_tool_span_measures_real_time(spans):
    """The whole reason this is a callback handler and not the AgentOps result sink."""
    import time

    tracer = genai_trace.SkipperTracer()
    tracer.on_tool_start({"name": "slow_tool"}, "{}", run_id="t")
    time.sleep(0.02)
    tracer.on_tool_end("ok", run_id="t")
    span = spans.get_finished_spans()[0]
    assert (span.end_time - span.start_time) >= 20_000_000  # ns


def test_a_tool_that_returns_its_failure_is_not_recorded_as_a_success(spans):
    """LangChain delivers a status="error" ToolMessage through on_tool_end, not on_tool_error."""

    class _Errored:
        status = "error"

    tracer = genai_trace.SkipperTracer()
    tracer.on_tool_start({"name": "flaky"}, "{}", run_id="t")
    tracer.on_tool_end(_Errored(), run_id="t")
    assert spans.get_finished_spans()[0].attributes["error.type"] == "tool_error"


def test_a_raised_tool_error_is_recorded(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_tool_start({"name": "boom"}, "{}", run_id="t")
    tracer.on_tool_error(ValueError("nope"), run_id="t")
    assert spans.get_finished_spans()[0].attributes["error.type"] == "ValueError"


def test_concurrent_runs_do_not_cross_their_spans(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_tool_start({"name": "a"}, "{}", run_id="ra")
    tracer.on_tool_start({"name": "b"}, "{}", run_id="rb")
    tracer.on_tool_end("ok", run_id="rb")
    tracer.on_tool_end("ok", run_id="ra")
    names = [s.attributes["gen_ai.request.model"] for s in spans.get_finished_spans()]
    assert names == ["b", "a"]


def test_an_end_without_a_start_is_ignored(spans):
    """LangGraph can replay or drop callbacks; an unpaired end must not raise."""
    genai_trace.SkipperTracer().on_tool_end("ok", run_id="never-started")
    assert spans.get_finished_spans() == ()


# ── fail-open ─────────────────────────────────────────────────────────────────


def test_a_broken_telemetry_module_never_breaks_a_callback(monkeypatch, spans):
    """Unimportable platform CLI ⇒ no spans and no exception, not a half-open span."""
    monkeypatch.setattr(genai_trace, "_genai", lambda: None)
    tracer = genai_trace.SkipperTracer()
    tracer.on_chat_model_start({"name": "m"}, [], run_id="r")
    tracer.on_llm_end(_Result([[]]), run_id="r")  # no span was opened; must be a no-op
    tracer.on_tool_start({"name": "t"}, "{}", run_id="r2")
    tracer.on_tool_end("ok", run_id="r2")
    assert spans.get_finished_spans() == ()
    assert tracer._spans == {}  # nothing left dangling


def test_spans_are_started_not_entered(monkeypatch, spans):
    """A callback handler must not make its span current — it would detach in another task."""
    from opentelemetry import trace

    tracer = genai_trace.SkipperTracer()
    tracer.on_tool_start({"name": "t"}, "{}", run_id="r")
    assert trace.get_current_span() is trace.INVALID_SPAN
    tracer.on_tool_end("ok", run_id="r")


# ── the guard ─────────────────────────────────────────────────────────────────


def test_every_turn_execution_goes_through_traced():
    """Clause 2 was half-implemented because one caller was instrumented and others were not.

    A ``graph.stream`` call whose config skips ``traced()`` is a turn that emits no span, and
    nothing else in the tree would report it.
    """
    agent = ROOT / "platform" / "services" / "agent" / "skipper"
    offenders = []
    for path in agent.rglob("*.py"):
        for line in path.read_text().splitlines():
            if re.search(r"\bgraph\.stream\(", line) and "traced(" not in line:
                offenders.append(f"{path.relative_to(ROOT)}: {line.strip()}")
    assert not offenders, "graph.stream() without traced(cfg):\n  " + "\n  ".join(offenders)


# ── against the real LangChain callback API ───────────────────────────────────


def test_langchain_actually_invokes_this_handler(spans):
    """The handler's value rests entirely on LangChain calling these method names.

    Every other test in this file drives the callbacks directly, so all of them would keep
    passing if LangChain renamed one and the handler went dead. This one runs a real model and
    a real tool through the real dispatch.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    cfg = genai_trace.traced({"configurable": {"thread_id": "t"}})
    FakeListChatModel(responses=["hi"]).invoke("hello", config=cfg)
    add.invoke({"a": 1, "b": 2}, config=cfg)

    kinds = {s.attributes["gen_ai.operation.name"] for s in spans.get_finished_spans()}
    assert kinds == {"chat", "tool"}
    tool_span = next(
        s for s in spans.get_finished_spans() if s.attributes["gen_ai.operation.name"] == "tool"
    )
    assert tool_span.attributes["gen_ai.request.model"] == "add"

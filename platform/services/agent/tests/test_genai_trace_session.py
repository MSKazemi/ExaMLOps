"""ADR 0021 decision 1 on Skipper's own spans: session id, step index and RETRIEVER spans.

Plus decision 2's missing link on the agent side: the entrypoint now installs a tracer provider,
without which every Skipper span went to OpenTelemetry's no-op provider.
"""

from __future__ import annotations

import importlib.util
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
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **k: provider.get_tracer("t"))
    yield exporter
    exporter.clear()


def test_every_span_carries_the_session_and_its_step(spans):
    tracer = genai_trace.SkipperTracer(session_id="owner:x:rw:t1")
    tracer.on_chat_model_start({"name": "m"}, [], run_id="llm")
    tracer.on_tool_start({"name": "a"}, "{}", run_id="ta")
    tracer.on_tool_start({"name": "b"}, "{}", run_id="tb")  # parallel tool calls
    tracer.on_tool_end("ok", run_id="tb")
    tracer.on_tool_end("ok", run_id="ta")
    tracer.on_llm_end(type("R", (), {"generations": [], "llm_output": None})(), run_id="llm")

    by_name = {s.attributes["gen_ai.request.model"]: s for s in spans.get_finished_spans()}
    assert {n: s.attributes["examlops.agent.step"] for n, s in by_name.items()} == {
        "m": 0,
        "a": 1,
        "b": 2,
    }  # the order the turn *started* them, distinct even when they overlap
    for s in by_name.values():
        assert s.attributes["session.id"] == "owner:x:rw:t1"
        assert s.attributes["gen_ai.conversation.id"] == "owner:x:rw:t1"
    assert by_name["a"].attributes["openinference.span.kind"] == "TOOL"


def test_traced_uses_the_turns_thread_id_as_the_session(spans):
    cfg = genai_trace.traced({"configurable": {"thread_id": "owner:x:rw:t9"}})
    handler = cfg["callbacks"][-1]
    assert handler.session_id == "owner:x:rw:t9"


def test_a_config_without_a_thread_has_no_session(spans):
    handler = genai_trace.traced({})["callbacks"][-1]
    handler.on_tool_start({"name": "t"}, "{}", run_id="r")
    handler.on_tool_end("ok", run_id="r")
    (s,) = spans.get_finished_spans()
    assert "session.id" not in s.attributes


def test_a_retriever_call_is_a_retriever_span_with_its_result_count(spans):
    tracer = genai_trace.SkipperTracer(session_id="s")
    tracer.on_retriever_start({"name": "kb"}, "secret question about alice", run_id="r")
    tracer.on_retriever_end(["doc1", "doc2"], run_id="r")
    (s,) = spans.get_finished_spans()
    assert s.attributes["gen_ai.operation.name"] == "retrieval"
    assert s.attributes["openinference.span.kind"] == "RETRIEVER"
    assert s.attributes["examlops.retrieval.documents"] == 2
    assert not any("alice" in str(v) for v in s.attributes.values())  # the query stays off


def test_a_retriever_error_is_recorded_and_ends_the_span(spans):
    tracer = genai_trace.SkipperTracer()
    tracer.on_retriever_start({"name": "kb"}, "q", run_id="r")
    tracer.on_retriever_error(TimeoutError("store down"), run_id="r")
    (s,) = spans.get_finished_spans()
    assert s.attributes["error.type"] == "TimeoutError"
    assert tracer._spans == {}


def test_langchain_dispatches_retriever_callbacks_to_this_handler(spans):
    """The retriever methods are only worth having if LangChain calls them by these names."""
    from langchain_core.callbacks import CallbackManagerForRetrieverRun
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever

    class _KB(BaseRetriever):
        def _get_relevant_documents(
            self, query: str, *, run_manager: CallbackManagerForRetrieverRun
        ) -> list[Document]:
            return [Document(page_content="flux")]

    cfg = genai_trace.traced({"configurable": {"thread_id": "t"}})
    _KB().invoke("which scheduler", config=cfg)
    (s,) = spans.get_finished_spans()
    assert s.attributes["openinference.span.kind"] == "RETRIEVER"
    assert s.attributes["examlops.retrieval.documents"] == 1
    assert s.attributes["session.id"] == "t"


# ── the entrypoint installs a provider ───────────────────────────────────────


def _entrypoint():
    path = Path(__file__).resolve().parents[1] / "agent_server.py"
    spec = importlib.util.spec_from_file_location("agent_server_tracing_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_entrypoint_installs_the_platform_tracer_provider(monkeypatch):
    from examlops import observability

    seen: list[str] = []
    monkeypatch.setattr(observability, "setup_tracing", lambda name: seen.append(name) or True)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert _entrypoint().bootstrap_tracing() is True
    assert seen == ["skipper"]
    source = (Path(__file__).resolve().parents[1] / "agent_server.py").read_text()
    assert source.index("bootstrap_tracing()\n") < source.index("uvicorn.run(")


def test_a_tracing_failure_never_stops_the_agent(monkeypatch, capsys):
    from examlops import observability

    def _boom(_name):
        raise RuntimeError("collector config broken")

    monkeypatch.setattr(observability, "setup_tracing", _boom)
    assert _entrypoint().bootstrap_tracing() is False
    assert "tracing not configured" in capsys.readouterr().err

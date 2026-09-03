# tests/unit/test_genai_serving_instrumentation.py
"""ADR 0006 clauses 2, 4 and 5 — the LLM-serving path, carbon, and the semconv opt-in.

Clause 2 named three boundaries; only the B2 gateway emitted a GenAI span, so anything
reaching a model any other way (``exa models engine``, a serving replica, a held engine)
was invisible. Clause 4 wanted carbon alongside cost and had only cost. Clause 5's
``OTEL_SEMCONV_STABILITY_OPT_IN`` appeared nowhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
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
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    yield exporter
    exporter.clear()


def _one(exporter):
    finished = exporter.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0]


class _Span:
    """Records attributes without an SDK, for the pure recording helpers."""

    def __init__(self) -> None:
        self.attrs: dict = {}

    def set_attribute(self, key, value) -> None:
        self.attrs[key] = value


# ── Clause 5: OTEL_SEMCONV_STABILITY_OPT_IN ───────────────────────────────────


def test_semconv_version_is_the_pinned_one_without_the_opt_in(monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    assert genai.latest_experimental_enabled() is False
    assert genai.semconv_version() == genai.SEMCONV_VERSION == "1.27.0"


def test_opt_in_is_one_token_of_a_comma_separated_list(monkeypatch):
    # OTel specifies a comma-separated list shared with other areas (http/dup &c.), so a
    # membership test is required — a whole-string comparison would silently ignore the
    # opt-in for anyone who also set an unrelated one.
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup, gen_ai_latest_experimental")
    assert genai.semconv_opt_in() == frozenset({"http/dup", "gen_ai_latest_experimental"})
    assert genai.latest_experimental_enabled() is True
    assert genai.semconv_version() == "latest-experimental"


def test_an_unrelated_opt_in_does_not_switch_conventions(monkeypatch):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")
    assert genai.latest_experimental_enabled() is False


def test_content_capture_uses_the_flat_attributes_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    monkeypatch.setenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", "true")
    span = _Span()
    assert genai.maybe_capture_content(span, prompt="hi", completion="hello") is True
    assert span.attrs["gen_ai.prompt"] == "hi"
    assert span.attrs["gen_ai.completion"] == "hello"
    assert "gen_ai.input.messages" not in span.attrs


def test_content_capture_uses_structured_messages_under_the_opt_in(monkeypatch):
    import json

    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    monkeypatch.setenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", "true")
    span = _Span()
    genai.maybe_capture_content(span, prompt="hi", completion="hello")
    assert json.loads(span.attrs["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "hi"}]}
    ]
    assert json.loads(span.attrs["gen_ai.output.messages"]) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}
    ]
    # Never both: capture is the one place content leaves the process.
    assert "gen_ai.prompt" not in span.attrs
    assert "gen_ai.completion" not in span.attrs


def test_the_opt_in_never_defeats_the_privacy_gate(monkeypatch):
    """The convention switch changes *where* content goes, never *whether* it is captured."""
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    monkeypatch.delenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", raising=False)
    span = _Span()
    assert genai.maybe_capture_content(span, prompt="secret", completion="x") is False
    assert span.attrs == {}


def test_structured_capture_still_passes_through_the_redaction_hook(monkeypatch):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    monkeypatch.setenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", "true")
    monkeypatch.setattr(genai, "_redactor", lambda text: text.replace("secret", "[REDACTED]"))
    span = _Span()
    genai.maybe_capture_content(span, prompt="my secret")
    assert "secret" not in span.attrs["gen_ai.input.messages"]
    assert "[REDACTED]" in span.attrs["gen_ai.input.messages"]


def test_the_span_reports_which_conventions_it_emitted(monkeypatch, spans):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    with genai.genai_span("model", system="vllm", model="m"):
        pass
    assert _one(spans).attributes["examlops.semconv.version"] == "latest-experimental"


# ── Clause 4: carbon ──────────────────────────────────────────────────────────


def test_no_device_hours_means_no_carbon_attribute():
    """No attribute beats a plausible one — the caller did not own any hardware."""
    span = _Span()
    assert genai.record_carbon(span) is None
    assert span.attrs == {}


def test_carbon_is_recorded_from_real_device_hours():
    span = _Span()
    estimate = genai.record_carbon(span, gpu_hours=0.5)
    assert estimate is not None
    from examlops.finops import carbon as carbon_mod

    expected = carbon_mod.estimate_carbon(0.5)
    assert span.attrs["examlops.energy.kwh"] == pytest.approx(expected["kwh"])
    assert span.attrs["examlops.carbon.co2e_g"] == pytest.approx(expected["co2e_g"])
    assert span.attrs["examlops.carbon.provider"]


def test_cpu_only_work_is_not_accounted_at_zero():
    """A CPU-only inference burned energy; the Green-AI model has a CPU term for it."""
    span = _Span()
    assert genai.record_carbon(span, cpu_hours=1.0) is not None
    assert span.attrs["examlops.carbon.co2e_g"] > 0


def test_an_unaccountable_input_records_its_reason_rather_than_nothing(monkeypatch):
    from examlops.finops import carbon as carbon_mod

    def _refuse(*_a, **_k):
        raise carbon_mod.CarbonInputUnaccounted("provider has no cpu_hours term")

    monkeypatch.setattr(carbon_mod, "estimate_carbon_via_provider", _refuse)
    span = _Span()
    assert genai.record_carbon(span, cpu_hours=1.0) is None
    assert "cpu_hours" in span.attrs["examlops.carbon.unaccounted"]
    assert "examlops.carbon.co2e_g" not in span.attrs


def test_a_broken_carbon_provider_never_breaks_the_call(monkeypatch):
    from examlops.finops import carbon as carbon_mod

    def _boom(*_a, **_k):
        raise RuntimeError("plugin exploded")

    monkeypatch.setattr(carbon_mod, "estimate_carbon_via_provider", _boom)
    assert genai.record_carbon(_Span(), gpu_hours=1.0) is None


# ── Clause 2: the serving path ────────────────────────────────────────────────


def test_build_engine_returns_an_instrumented_engine():
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    assert isinstance(eng, engines.InstrumentedEngine)
    # Identity and the contract are unchanged.
    assert eng.name == "echo"
    assert eng.health() is True
    assert isinstance(eng, engines.InferenceEngine)


def test_the_wrapper_delegates_attributes_it_does_not_instrument():
    cfg = engines.EngineConfig(engine="echo", speculative_decoding={"enabled": True})
    eng = engines.build_engine(cfg)
    assert eng.config.speculative_decoding == {"enabled": True}
    assert eng.engine.__class__ is engines.EchoEngine


def test_wrapping_a_text_only_engine_does_not_invent_a_chat_surface():
    """``supports_chat`` decides pass-through vs flatten; a blanket ``chat`` loses media."""
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    assert engines.supports_chat(eng) is False


def test_a_chat_capable_engine_keeps_its_chat_surface():
    class _Chatty:
        name = "vllm-server"

        def generate(self, prompt, **kw):
            return engines.Completion(text=prompt)

        def stream(self, prompt, **kw):
            yield prompt

        def chat(self, messages, **kw):
            return engines.Completion(text=messages[-1]["content"], completion_tokens=1)

        def chat_stream(self, messages, **kw):
            yield messages[-1]["content"]

        def health(self):
            return True

    eng = engines.instrument(_Chatty(), "m")
    assert engines.supports_chat(eng) is True
    assert eng.chat([{"role": "user", "content": "hi"}]).text == "hi"
    assert list(eng.chat_stream([{"role": "user", "content": "hi"}])) == ["hi"]


def test_instrumenting_twice_does_not_nest_wrappers():
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    assert engines.instrument(eng, "m") is eng


def test_generate_emits_a_model_span_with_usage(spans):
    eng = engines.build_engine(engines.EngineConfig(engine="echo"), model_path="my-llm")
    eng.generate("one two three four", max_tokens=3)
    span = _one(spans)
    assert span.attributes["gen_ai.operation.name"] == "model"
    assert span.attributes["gen_ai.system"] == "echo"
    assert span.attributes["gen_ai.request.model"] == "my-llm"
    assert span.attributes["gen_ai.usage.input_tokens"] == 4
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert span.attributes["gen_ai.response.finish_reasons"] == ("stop",)


def test_the_span_encloses_the_call_so_its_duration_is_the_latency(spans):
    """The gateway's span is opened after the fact; these wrap the call, so time is real."""
    import time

    class _Slow:
        name = "echo"

        def generate(self, prompt, **kw):
            time.sleep(0.02)
            return engines.Completion(text=prompt)

        def stream(self, prompt, **kw):
            yield prompt

        def health(self):
            return True

    engines.instrument(_Slow(), "m").generate("x")
    span = _one(spans)
    assert (span.end_time - span.start_time) >= 20_000_000  # ns


def test_a_local_engine_records_carbon_from_its_measured_device_time(spans):
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    eng.generate("one two", max_tokens=2)
    attrs = _one(spans).attributes
    assert attrs["examlops.carbon.co2e_g"] > 0
    assert attrs["examlops.energy.kwh"] > 0


def test_a_server_engine_records_no_carbon(spans):
    """Its GPU is shared by every concurrent client; per-request wall-clock overcounts it."""

    class _Server:
        name = "vllm-server"

        def generate(self, prompt, **kw):
            return engines.Completion(text=prompt)

        def stream(self, prompt, **kw):
            yield prompt

        def health(self):
            return True

    engines.instrument(_Server(), "m").generate("x")
    assert "examlops.carbon.co2e_g" not in _one(spans).attributes


def test_a_stream_reports_chunks_and_never_a_token_count(spans):
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    assert list(eng.stream("a b c", max_tokens=2)) == ["a ", "b "]
    attrs = _one(spans).attributes
    assert attrs["examlops.stream.chunks"] == 2
    assert "gen_ai.usage.output_tokens" not in attrs


def test_an_abandoned_stream_still_closes_its_span(spans):
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    gen = eng.stream("a b c d", max_tokens=4)
    next(gen)
    gen.close()
    assert len(spans.get_finished_spans()) == 1


def test_an_engine_failure_propagates_and_is_recorded(spans):
    class _Broken:
        name = "echo"

        def generate(self, prompt, **kw):
            raise RuntimeError("engine down")

        def stream(self, prompt, **kw):
            yield ""

        def health(self):
            return True

    with pytest.raises(RuntimeError, match="engine down"):
        engines.instrument(_Broken(), "m").generate("x")
    assert _one(spans).events  # the exception was recorded on the span


def test_broken_telemetry_never_breaks_the_completion(monkeypatch, spans):
    monkeypatch.setattr(
        genai, "record_usage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("otel down"))
    )
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    assert eng.generate("one two", max_tokens=2).text == "one two"


def test_instrumentation_is_a_no_op_when_tracing_is_disabled(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    eng = engines.build_engine(engines.EngineConfig(engine="echo"))
    assert eng.generate("one two", max_tokens=2).text == "one two"
    assert list(eng.stream("one two", max_tokens=1)) == ["one "]


def test_the_gateway_engine_edge_is_instrumented_end_to_end(spans):
    """R-A1: gateway → build_engine. The backend call now carries a model span of its own."""
    from examlops import gateway as gw

    router = gw.build_engine_router("my-llm", engines.EngineConfig(engine="echo"))
    gw.GatewayClient(router).chat("my-llm", [{"role": "user", "content": "hi there"}])
    names = [s.attributes["gen_ai.operation.name"] for s in spans.get_finished_spans()]
    assert "model" in names  # the engine span, which did not exist before

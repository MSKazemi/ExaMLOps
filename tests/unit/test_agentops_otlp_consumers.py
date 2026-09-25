"""ADR 0021 decision 2: Langfuse / Phoenix as OTel consumers, over OTLP/HTTP.

Langfuse ingests OTLP over HTTP only; the platform's exporter was gRPC-only, so reaching Langfuse
needed a collector hop nobody had built. These tests run the real exporter against a loopback
HTTP server that decodes the OTLP protobuf the way a collector does — a faithful stand-in for
Langfuse's ``/api/public/otel/v1/traces`` and Phoenix's ``/v1/traces``. No live Langfuse or
Phoenix is involved; the claim is exactly "a spec-conformant OTLP/HTTP receiver gets these spans
with these attributes and this auth".
"""

from __future__ import annotations

import base64
import gzip
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import observability  # noqa: E402
from examlops.telemetry import genai, otlp_http  # noqa: E402

_CONSUMER_ENV = (
    "EXAMLOPS_OTEL_CONSUMERS",
    "LANGFUSE_HOST",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_API_KEY",
    "EXAMLOPS_OTEL_ALLOW_INSECURE",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _CONSUMER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(otlp_http.time, "sleep", lambda _s: None)  # no real backoff in tests


class _Collector:
    """Loopback OTLP/HTTP receiver: records path, headers and decoded spans per request."""

    def __init__(self, statuses: list[int] | None = None, redirect_to: str | None = None):
        self.requests: list[dict] = []
        self._statuses = list(statuses or [])
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                    ExportTraceServiceRequest,
                )

                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                req = ExportTraceServiceRequest()
                req.ParseFromString(body)
                spans = [
                    {
                        "name": s.name,
                        "attributes": {
                            kv.key: getattr(kv.value, kv.value.WhichOneof("value"))
                            for kv in s.attributes
                        },
                    }
                    for rs in req.resource_spans
                    for ss in rs.scope_spans
                    for s in ss.spans
                ]
                collector.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "spans": spans,
                    }
                )
                status = collector._statuses.pop(0) if collector._statuses else 200
                if redirect_to:
                    self.send_response(307)
                    self.send_header("Location", redirect_to)
                    self.end_headers()
                    return
                self.send_response(status)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *_a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    c = _Collector()
    yield c
    c.close()


def _provider_with(exporter):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def _agent_tool_span(monkeypatch, provider):
    from opentelemetry import trace

    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **k: provider.get_tracer("t"))
    with genai.genai_span("tool", system="skipper", model="get_status") as span:
        genai.set_agent_context(span, session_id="owner:abc:rw:t1", step=2)
        genai.record_usage(span, model="llama3.1:8b", input_tokens=10, output_tokens=4)


# ── the exporter ──────────────────────────────────────────────────────────────


def test_the_fallback_exporter_delivers_decodable_otlp_protobuf(monkeypatch, collector):
    exporter = otlp_http.OTLPHttpSpanExporter(collector.url + "/v1/traces", headers={"x-k": "v"})
    _agent_tool_span(monkeypatch, _provider_with(exporter))

    (req,) = collector.requests
    assert req["path"] == "/v1/traces"
    assert req["headers"]["content-type"] == "application/x-protobuf"
    assert req["headers"]["x-k"] == "v"
    (span,) = req["spans"]
    attrs = span["attributes"]
    assert attrs["gen_ai.operation.name"] == "tool"
    assert attrs["openinference.span.kind"] == "TOOL"
    assert attrs["session.id"] == "owner:abc:rw:t1"
    assert attrs["examlops.agent.step"] == 2
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert exporter.exported == 1 and exporter.failed == 0


def test_a_retryable_status_is_retried_and_then_succeeds(monkeypatch):
    c = _Collector(statuses=[503, 200])
    try:
        exporter = otlp_http.OTLPHttpSpanExporter(c.url + "/v1/traces")
        _agent_tool_span(monkeypatch, _provider_with(exporter))
        assert len(c.requests) == 2 and exporter.exported == 1
    finally:
        c.close()


def test_a_rejected_batch_is_not_retried_and_is_counted(monkeypatch):
    c = _Collector(statuses=[400, 200])
    try:
        exporter = otlp_http.OTLPHttpSpanExporter(c.url + "/v1/traces")
        _agent_tool_span(monkeypatch, _provider_with(exporter))
        assert len(c.requests) == 1 and exporter.failed == 1 and exporter.exported == 0
    finally:
        c.close()


def test_attempts_are_bounded_when_the_collector_stays_down(monkeypatch):
    c = _Collector(statuses=[503] * 10)
    try:
        exporter = otlp_http.OTLPHttpSpanExporter(c.url + "/v1/traces")
        _agent_tool_span(monkeypatch, _provider_with(exporter))
        assert len(c.requests) == otlp_http.MAX_ATTEMPTS and exporter.failed == 1
    finally:
        c.close()


def test_a_redirect_is_never_followed_with_the_credential(monkeypatch):
    target = _Collector()
    c = _Collector(redirect_to=target.url + "/v1/traces")
    try:
        exporter = otlp_http.OTLPHttpSpanExporter(
            c.url + "/v1/traces", headers={"authorization": "Basic c2VjcmV0"}
        )
        _agent_tool_span(monkeypatch, _provider_with(exporter))
        assert target.requests == []
        assert exporter.failed == 1
    finally:
        c.close()
        target.close()


def test_an_unreachable_collector_fails_the_batch_without_raising(monkeypatch):
    exporter = otlp_http.OTLPHttpSpanExporter("http://127.0.0.1:9/v1/traces", timeout=0.5)
    _agent_tool_span(monkeypatch, _provider_with(exporter))
    assert exporter.failed == 1


def test_a_shut_down_exporter_exports_nothing(monkeypatch, collector):
    from opentelemetry.sdk.trace.export import SpanExportResult

    exporter = otlp_http.OTLPHttpSpanExporter(collector.url + "/v1/traces")
    exporter.shutdown()
    assert exporter.export([object()]) is SpanExportResult.FAILURE
    assert collector.requests == []


# ── consumer configuration (fail closed) ─────────────────────────────────────


def test_nothing_is_exported_to_a_consumer_that_is_not_listed(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert otlp_http.consumer_targets() == ([], [])


def test_langfuse_gets_its_otlp_path_and_basic_auth(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "langfuse")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example/")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    (target,), problems = otlp_http.consumer_targets()
    assert problems == []
    assert target.endpoint == "https://langfuse.example/api/public/otel/v1/traces"
    token = base64.b64decode(target.headers["authorization"].split(" ", 1)[1]).decode()
    assert token == "pk-lf-1:sk-lf-2"
    assert "sk-lf-2" not in repr(target)


def test_langfuse_has_no_default_saas_host(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "langfuse")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    targets, problems = otlp_http.consumer_targets()
    assert targets == [] and problems == ["langfuse: LANGFUSE_HOST not set"]


def test_a_credential_is_never_sent_over_plain_http_to_a_remote_host(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "langfuse,phoenix")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse.internal:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix.internal:6006")
    monkeypatch.setenv("PHOENIX_API_KEY", "key")
    targets, problems = otlp_http.consumer_targets()
    assert targets == []
    assert all("refusing to send a credential over plain HTTP" in p for p in problems)
    assert len(problems) == 2


def test_insecure_is_an_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "langfuse")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse.internal:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("EXAMLOPS_OTEL_ALLOW_INSECURE", "1")
    (target,), _ = otlp_http.consumer_targets()
    assert target.endpoint.startswith("http://langfuse.internal")


def test_keyless_phoenix_may_use_plain_http_and_gets_the_traces_path(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "phoenix")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix.internal:6006")
    (target,), problems = otlp_http.consumer_targets()
    assert problems == [] and target.endpoint == "http://phoenix.internal:6006/v1/traces"
    assert target.headers == {}


def test_an_unknown_consumer_is_reported_not_guessed(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "agentops-saas")
    targets, problems = otlp_http.consumer_targets()
    assert targets == [] and "unknown consumer" in problems[0]


def test_header_parsing_follows_the_otel_env_format():
    assert otlp_http.parse_headers("Authorization=Bearer%20t,x-a=b,,bad") == {
        "authorization": "Bearer t",
        "x-a": "b",
    }


# ── setup_tracing wires it all ────────────────────────────────────────────────


def _captured_provider(monkeypatch, service="skipper"):
    from opentelemetry import trace

    captured: list = []
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: object())  # "not configured yet"
    monkeypatch.setattr(trace, "set_tracer_provider", captured.append)
    assert observability.setup_tracing(service) is True
    (provider,) = captured
    return provider


def test_langfuse_receives_the_agents_spans_beside_the_primary_exporter(monkeypatch, collector):
    """End to end through setup_tracing: a Langfuse-shaped receiver on loopback gets the span,
    at Langfuse's path, with Basic auth; the primary pipeline is still configured."""
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "langfuse")
    monkeypatch.setenv("LANGFUSE_HOST", collector.url)  # loopback: plain HTTP is allowed
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    primary = _Collector()  # stands in for Tempo, so the primary pipeline is observable too
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", primary.url)
    try:
        provider = _captured_provider(monkeypatch)
        processors = provider._active_span_processor._span_processors
        assert len(processors) == 2  # primary + langfuse

        _agent_tool_span(monkeypatch, provider)
        provider.force_flush()
        assert len(primary.requests) == 1  # OTel stays the source of truth
    finally:
        primary.close()

    (req,) = collector.requests
    assert req["path"] == "/api/public/otel/v1/traces"
    assert req["headers"]["authorization"] == "Basic " + base64.b64encode(b"pk-lf:sk-lf").decode()
    assert req["spans"][0]["attributes"]["session.id"] == "owner:abc:rw:t1"
    provider.shutdown()


def test_the_primary_exporter_can_speak_otlp_http(monkeypatch, collector):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-tenant=acme")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    provider = _captured_provider(monkeypatch)

    _agent_tool_span(monkeypatch, provider)
    provider.force_flush()

    (req,) = collector.requests
    assert req["path"] == "/v1/traces" and req["headers"]["x-tenant"] == "acme"
    provider.shutdown()


def test_a_misconfigured_consumer_never_stops_the_service(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OTEL_CONSUMERS", "langfuse")  # no host, no keys
    provider = _captured_provider(monkeypatch)
    assert len(provider._active_span_processor._span_processors) == 1  # primary only
    provider.shutdown()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "grpc"), ("http/protobuf", "http/protobuf"), ("http/json", "grpc"), ("GRPC", "grpc")],
)
def test_protocol_selection(monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", raw)
    assert observability.otlp_protocol() == expected


def test_the_signal_specific_endpoint_is_used_as_is(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://c.example/custom")
    assert observability._http_traces_endpoint() == "https://c.example/custom"
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://c.example/")
    assert observability._http_traces_endpoint() == "https://c.example/v1/traces"

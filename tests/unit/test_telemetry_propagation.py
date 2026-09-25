"""ADR 0148 decision 1 — W3C trace context through the platform's outbound hops."""

from __future__ import annotations

import pytest

from examlops.telemetry import propagation as prop

TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _otel_off(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (TP, True),
        (TP.upper(), True),
        ("00-00000000000000000000000000000000-00f067aa0ba902b7-01", False),  # zero trace id
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01", False),  # zero parent id
        ("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", False),  # forbidden version
        ("garbage", False),
        (None, False),
    ],
)
def test_traceparent_validation(value, ok):
    assert prop.valid_traceparent(value) is ok


def test_no_context_adds_nothing():
    assert prop.inject_http_headers({"A": "b"}) == {"A": "b"}
    assert prop.inject_mcp_meta({"name": "t"}) == {"name": "t"}


def test_inbound_context_is_forwarded_over_http_and_mcp():
    with prop.bind_inbound({"TraceParent": TP, "tracestate": "k=v"}):
        headers = prop.inject_http_headers({})
        params = prop.inject_mcp_meta({"name": "t", "arguments": {}})
    assert headers == {"traceparent": TP, "tracestate": "k=v"}
    assert params["_meta"] == {"traceparent": TP, "tracestate": "k=v"}
    assert prop.current_context() == {}  # unbound after the handler


def test_malformed_inbound_is_dropped_not_forwarded():
    with prop.bind_inbound({"traceparent": "00-zz-yy-01"}):
        assert prop.inject_http_headers({}) == {}


def test_oversized_tracestate_is_not_forwarded():
    with prop.bind_inbound({"traceparent": TP, "tracestate": "x" * 600}):
        assert prop.current_context() == {"traceparent": TP}


@pytest.mark.parametrize("bad", ["k=v\r\nX-Injected: 1", "k=v\n", "k=\x00v", "k=v\x7f"])
def test_a_tracestate_with_control_characters_is_not_forwarded(bad):
    # tracestate is attacker-supplied inbound and re-emitted as an outbound header value.
    with prop.bind_inbound({"traceparent": TP, "tracestate": bad}):
        assert prop.current_context() == {"traceparent": TP}


def test_caller_supplied_traceparent_wins():
    mine = "00-11111111111111111111111111111111-2222222222222222-01"
    with prop.bind_inbound({"traceparent": TP}):
        assert prop.inject_http_headers({"traceparent": mine})["traceparent"] == mine
        meta = prop.inject_mcp_meta({"_meta": {"traceparent": mine, "progressToken": 1}})["_meta"]
    assert meta == {"traceparent": mine, "progressToken": 1}


def test_active_otel_span_is_propagated(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("invoke_agent") as span:
        ctx = prop.current_context()
        trace_id = format(span.get_span_context().trace_id, "032x")
    assert prop.valid_traceparent(ctx["traceparent"])
    assert ctx["traceparent"].split("-")[1] == trace_id


def test_server_engine_carries_the_trace_to_vllm():
    from examlops.engines.vllm_server import VLLMServerEngine

    eng = VLLMServerEngine("http://h:8000", "m", api_key="")
    with prop.bind_inbound({"traceparent": TP}):
        assert eng._headers()["traceparent"] == TP
    assert "traceparent" not in eng._headers()

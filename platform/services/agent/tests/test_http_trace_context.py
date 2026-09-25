"""ADR 0148 d1: the agent's outbound calls (including OIP ``predict``) carry W3C trace context."""

import httpx
import pytest
import respx
from skipper.tools import _http

from examlops.telemetry.propagation import bind_inbound

TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _otel_off(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


@respx.mock
def test_bound_trace_context_reaches_the_model_server():
    route = respx.post("http://ray/v2/models/jpcp/infer").mock(
        return_value=httpx.Response(200, json={"outputs": []})
    )
    with bind_inbound({"traceparent": TP}):
        _, err = _http.request_json("ray_serve", "POST", "http://ray/v2/models/jpcp/infer", json={})
    assert err is None
    assert route.calls.last.request.headers["traceparent"] == TP


@respx.mock
def test_no_context_sends_no_traceparent():
    route = respx.get("http://svc/x").mock(return_value=httpx.Response(200, json={}))
    _http.request_json("svc", "GET", "http://svc/x")
    assert "traceparent" not in route.calls.last.request.headers


@respx.mock
def test_a_callers_own_traceparent_is_kept():
    mine = "00-11111111111111111111111111111111-2222222222222222-01"
    route = respx.get("http://svc/x").mock(return_value=httpx.Response(200, json={}))
    with bind_inbound({"traceparent": TP}):
        _http.request_json("svc", "GET", "http://svc/x", headers={"traceparent": mine})
    assert route.calls.last.request.headers["traceparent"] == mine

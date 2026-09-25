"""The deployed llm-gateway SERVICE as a `GatewayClient` candidate backend (ADR 0151 d4).

Before this, the "default" model route only ever echoed — even with a real, reachable
`llm-gateway` service configured — because `GatewayClient`/`build_default_router` never called it.
`EXAMLOPS_LLM_GATEWAY_URL` now makes the deployed service the *first* candidate for the default
route; echo (and, when set, the direct Ollama routes `add_ollama_routes` already builds) remain
the break-glass fallback — the in-process library is "the last resort" the ADR promises, never
the other way around.
"""

from __future__ import annotations

import json

import httpx
import pytest

from examlops.gateway import (
    Completion,
    GatewayClient,
    build_default_router,
    gateway_service_backend,
)
from examlops.gateway.egress import EgressDenied


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


def make_backend(handler, **kw):
    kw.setdefault("key", "vk-test")
    return gateway_service_backend("http://gw", transport=httpx.MockTransport(handler), **kw)


def req(model="default"):
    return model, [{"role": "user", "content": "hello there"}]


# ── the backend itself ─────────────────────────────────────────────────────────


def test_posts_openai_shaped_body_to_v1_chat_completions_with_the_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _resp(
            200,
            {
                "id": "chatcmpl-1",
                "model": "qwen3:8b",
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    backend = make_backend(handler)
    comp = backend(*req(), temperature=0.2, max_tokens=16)

    assert seen["url"] == "http://gw/v1/chat/completions"
    assert seen["auth"] == "Bearer vk-test"
    assert seen["body"]["model"] == "default"
    assert seen["body"]["temperature"] == 0.2 and seen["body"]["max_tokens"] == 16
    assert isinstance(comp, Completion)
    assert comp.text == "hi" and comp.backend == "llm-gateway"
    assert (comp.prompt_tokens, comp.completion_tokens) == (3, 2)


def test_no_key_omits_the_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _resp(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    make_backend(handler, key=None)(*req())
    assert seen["auth"] is None


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "model_not_found"), (429, "rate_limited"), (503, "upstream_unavailable")],
)
def test_a_typed_gateway_error_is_raised_with_its_code(status, code):
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(
            status,
            {"error": {"message": "boom", "code": code, "request_id": "req_abc"}},
        )

    with pytest.raises(RuntimeError) as ei:
        make_backend(handler)(*req())
    assert code in str(ei.value)


def test_an_unreachable_service_raises_a_plain_exception_not_a_hang():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(Exception, match="refused|unreachable|connect"):
        make_backend(handler)(*req())


def test_a_malformed_response_body_raises_rather_than_returning_garbage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(Exception):  # noqa: PT011 - any parse failure is acceptable here
        make_backend(handler)(*req())


# ── wiring into build_default_router (ADR 0151 d4) ──────────────────────────────


def test_unset_env_leaves_the_default_route_pure_echo(monkeypatch):
    """No behaviour change unless configured — the same discipline as every other gateway knob."""
    monkeypatch.delenv("EXAMLOPS_LLM_GATEWAY_URL", raising=False)
    router = build_default_router(endpoints=False)
    comp = GatewayClient(router, guardrail=None).chat(
        "default", [{"role": "user", "content": "hi"}]
    )
    assert comp.backend == "echo" and comp.text == "hi"


def test_configured_service_answers_the_default_route_before_echo(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(
            200,
            {"choices": [{"message": {"content": "real answer"}, "finish_reason": "stop"}]},
        )

    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setattr("examlops.gateway._service_transport", lambda: httpx.MockTransport(handler))
    router = build_default_router(endpoints=False)
    comp = GatewayClient(router, guardrail=None).chat(
        "default", [{"role": "user", "content": "hi"}]
    )
    assert comp.backend == "llm-gateway" and comp.text == "real answer"


def test_a_down_service_still_falls_back_to_echo_not_an_error(monkeypatch):
    """R11 (last-resort): the in-process route is break-glass, so a dead service degrades to
    echo rather than making the whole gateway unusable — the same as any other backend failover."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setattr("examlops.gateway._service_transport", lambda: httpx.MockTransport(handler))
    router = build_default_router(endpoints=False)
    comp = GatewayClient(router, guardrail=None).chat(
        "default", [{"role": "user", "content": "hi"}]
    )
    assert comp.backend == "echo"  # degraded, not raised


def test_the_gateway_key_env_var_is_forwarded(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _resp(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_KEY", "vk-from-env")
    monkeypatch.setattr("examlops.gateway._service_transport", lambda: httpx.MockTransport(handler))
    router = build_default_router(endpoints=False)
    GatewayClient(router, guardrail=None).chat("default", [{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer vk-from-env"


def test_only_the_default_route_is_affected_named_routes_are_untouched(monkeypatch):
    """This wiring must not silently redirect an explicitly-named model to the service — a
    registered endpoint or an Ollama-discovered model keeps answering from its own backend."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, {"choices": [{"message": {"content": "wrong"}, "finish_reason": "stop"}]})

    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setattr("examlops.gateway._service_transport", lambda: httpx.MockTransport(handler))
    router = build_default_router(endpoints=False)
    router.add_route(
        "qwen3:8b", [("direct", lambda m, msgs, **kw: Completion("direct answer", m, "direct"))]
    )
    comp = GatewayClient(router, guardrail=None).chat(
        "qwen3:8b", [{"role": "user", "content": "hi"}]
    )
    assert comp.backend == "direct" and comp.text == "direct answer"


# ── egress validation (ADR 0154) ─────────────────────────────────────────────
#
# `gateway_service_backend` reaches a URL an environment variable names, exactly the shape ADR
# 0154 exists to police — a misconfigured or attacker-influenced `EXAMLOPS_LLM_GATEWAY_URL` must
# not be able to point prompts at an Azure endpoint or a cloud-metadata address just because this
# is the "in-process fallback" path rather than a full `Provider`.


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.openai.azure.com/openai/v1",
        "https://x.cognitiveservices.azure.com",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/",
    ],
)
def test_gateway_service_backend_refuses_a_forbidden_target(url):
    with pytest.raises(EgressDenied):
        gateway_service_backend(url)


def test_gateway_service_backend_allows_ordinary_local_and_site_addresses():
    for url in ("http://127.0.0.1:18020", "http://llm-gateway:8020", "http://10.0.0.5:8020"):
        assert callable(gateway_service_backend(url))  # constructed a working Backend, not refused


def test_build_default_router_degrades_to_echo_when_the_configured_url_is_forbidden(monkeypatch):
    """The same fail-soft contract `add_ollama_routes` already has: a bad env var must not stop
    `build_default_router` from returning a working table — it must warn and fall through."""
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    router = build_default_router(endpoints=False)  # must not raise
    comp = GatewayClient(router, guardrail=None).chat(
        "default", [{"role": "user", "content": "hi"}]
    )
    assert comp.backend == "echo"

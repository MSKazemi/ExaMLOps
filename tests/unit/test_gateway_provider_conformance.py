"""Shared provider conformance suite (design spec §12.2; PLAN.md P1's last big item).

Every provider adapter already has its own extensive fault-injection tests
(`test_gateway_ollama_provider.py`, `test_gateway_openai_compat_provider.py`) — those prove each
adapter, read alone, handles what it claims to. What none of them can prove, because each only
ever looks at one adapter, is the property `GatewayCore`'s whole failover/breaker design actually
depends on: that a caller (and the routing core) sees the **same classified outcome** — the same
`ProviderError.kind`/`.retryable` — for the same class of upstream misbehaviour, *regardless of
which adapter served the request*. A router that fails over from an Ollama deployment to an
openai_compat one must not discover that the two disagree about what a "the server is down" fault
even is.

This module defines one canonical set of fault scenarios and drives every registered adapter
through the identical assertion for each — new adapters are added by extending `PROVIDERS` below,
not by writing a parallel copy of these tests.

Scope note: this covers non-streaming chat's error classification and successful-response shape —
the highest-value, most-drift-prone part (spec §12.2 also names streaming, tool calls, JSON
schema and embed conformance; those are tracked as follow-up, not silently assumed covered).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from examlops.gateway.providers.base import ChatRequest, Provider, ProviderError
from examlops.gateway.providers.ollama import OllamaProvider
from examlops.gateway.providers.openai_compat import OpenAICompatProvider


def _ollama(fault: str | None) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(fault, native=True)

    return OllamaProvider("n1", "http://ollama.test", transport=httpx.MockTransport(handler))


def _openai_compat(fault: str | None) -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(fault, native=False)

    return OpenAICompatProvider(
        "router", "http://router.test", transport=httpx.MockTransport(handler)
    )


def _response(fault: str | None, *, native: bool) -> httpx.Response:
    """One upstream response (or a raised transport error) per named fault class, in whichever
    wire shape the calling adapter (`native=True` for Ollama, `False` for openai_compat) expects.
    `fault=None` is the successful-response conformance case."""
    if fault == "connect_refused":
        raise httpx.ConnectError("refused")
    if fault == "timeout":
        raise httpx.ReadTimeout("slow")
    if fault == "malformed_body":
        return httpx.Response(200, text="not json")
    if fault == "429_retry_after":
        body = {"error": "slow"}
        return httpx.Response(429, headers={"Retry-After": "3"}, json=body)
    if fault == "5xx":
        body = {"error": "busy"} if native else {"error": {"message": "busy"}}
        return httpx.Response(503, json=body)
    if fault == "model_not_found":
        body = {"error": "model not found"} if native else {"error": {"message": "not found"}}
        return httpx.Response(404, json=body)
    if fault is None:
        if native:
            return httpx.Response(
                200,
                json={
                    "message": {"role": "assistant", "content": "hi"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 3,
                    "eval_count": 2,
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"index": 0, "message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )
    raise AssertionError(f"unhandled fault {fault!r} in the conformance fixture itself")


#: name → factory. Adding a new provider adapter to this suite is adding one entry here.
PROVIDERS: dict[str, Callable[[str | None], Provider]] = {
    "ollama": _ollama,
    "openai_compat": _openai_compat,
}


def req(**kw: Any) -> ChatRequest:
    kw.setdefault("model", "m")
    kw.setdefault("messages", [{"role": "user", "content": "hi"}])
    return ChatRequest(**kw)


# ── error-class conformance ─────────────────────────────────────────────────────


@pytest.mark.parametrize("provider_name", list(PROVIDERS))
@pytest.mark.parametrize(
    ("fault", "expected_kind", "expected_retryable"),
    [
        ("connect_refused", "upstream_unavailable", True),
        ("timeout", "upstream_timeout", True),
        ("malformed_body", "upstream_error", True),
        ("429_retry_after", "rate_limited", True),
        ("5xx", "upstream_error", True),
        ("model_not_found", "model_not_found", False),
    ],
)
async def test_every_provider_classifies_the_same_fault_the_same_way(
    provider_name, fault, expected_kind, expected_retryable
):
    provider = PROVIDERS[provider_name](fault)
    with pytest.raises(ProviderError) as ei:
        await provider.chat(req())
    assert ei.value.kind == expected_kind, (
        f"{provider_name} classified {fault!r} as {ei.value.kind!r}, "
        f"every adapter must agree on {expected_kind!r}"
    )
    assert ei.value.retryable is expected_retryable
    assert ei.value.provider == provider.name  # every adapter must self-identify in its own error


async def test_every_provider_reports_the_429_retry_after_the_same_way():
    for name, factory in PROVIDERS.items():
        provider = factory("429_retry_after")
        with pytest.raises(ProviderError) as ei:
            await provider.chat(req())
        assert ei.value.retry_after == 3.0, f"{name} did not surface Retry-After consistently"


# ── successful-response shape conformance ───────────────────────────────────────


@pytest.mark.parametrize("provider_name", list(PROVIDERS))
async def test_every_provider_returns_the_same_result_shape_on_success(provider_name):
    provider = PROVIDERS[provider_name](None)
    result = await provider.chat(req())
    assert result.text == "hi"
    assert result.provider == provider.name
    assert result.finish_reason == "stop"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (3, 2)
    assert result.usage.estimated is False  # both adapters received real counts here


# ── this fixture's own honesty ──────────────────────────────────────────────────


def test_every_declared_provider_is_actually_exercised():
    """A guard against the class of bug this whole module exists to prevent in the adapters
    themselves: `PROVIDERS` silently losing an entry (e.g. a bad merge) would make every
    parametrized test above pass having quietly stopped checking one adapter."""
    assert set(PROVIDERS) == {"ollama", "openai_compat"}


def test_the_fixture_itself_raises_on_an_unhandled_fault_name():
    """A typo'd fault name in a future parametrize entry must fail loudly in the fixture, not
    silently return `None`'s success response and make every provider "agree" for the wrong
    reason."""
    with pytest.raises(AssertionError, match="unhandled fault"):
        _response("no-such-fault", native=True)


# ── streaming conformance (design spec §12.2's "streaming" dimension) ──────────────
#
# A pre-connection fault (a 4xx before the first byte, a connect refusal) reuses the exact same
# `_from_response`/`_from_exception` code path as non-streaming chat — already conformance-tested
# above — so this section covers only what streaming adds: the "has the first byte already gone
# out" distinction (ADR 0153 d9), which changes both the error `kind` and whether it's retryable.


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _ollama_stream(fault: str) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if fault == "error_before_commit":
            lines = [json.dumps({"error": "boom"})]
        elif fault == "error_after_commit":
            lines = [
                json.dumps({"message": {"role": "assistant", "content": "partial"}}),
                json.dumps({"error": "boom"}),
            ]
        elif fault == "malformed_after_commit":
            lines = [
                json.dumps({"message": {"role": "assistant", "content": "partial"}}),
                "not json",
            ]
        else:
            raise AssertionError(f"unhandled streaming fault {fault!r}")
        return httpx.Response(200, content=("\n".join(lines) + "\n").encode())

    return OllamaProvider("n1", "http://ollama.test", transport=httpx.MockTransport(handler))


def _openai_compat_stream(fault: str) -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if fault == "error_before_commit":
            body = _sse({"error": {"message": "boom"}})
        elif fault == "error_after_commit":
            body = _sse(
                {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
                {"error": {"message": "boom"}},
            )
        elif fault == "malformed_after_commit":
            body = (
                _sse(
                    {
                        "choices": [
                            {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                        ]
                    }
                )
                + b"data: not json\n\n"
            )
        else:
            raise AssertionError(f"unhandled streaming fault {fault!r}")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return OpenAICompatProvider(
        "router", "http://router.test", transport=httpx.MockTransport(handler)
    )


STREAM_PROVIDERS: dict[str, Callable[[str], Provider]] = {
    "ollama": _ollama_stream,
    "openai_compat": _openai_compat_stream,
}


@pytest.mark.parametrize("provider_name", list(STREAM_PROVIDERS))
@pytest.mark.parametrize(
    ("fault", "expected_kind", "expected_retryable"),
    [
        ("error_before_commit", "upstream_error", True),
        ("error_after_commit", "stream_interrupted", False),
        ("malformed_after_commit", "stream_interrupted", False),
    ],
)
async def test_every_provider_classifies_the_same_streaming_fault_the_same_way(
    provider_name, fault, expected_kind, expected_retryable
):
    provider = STREAM_PROVIDERS[provider_name](fault)
    chunks = []
    with pytest.raises(ProviderError) as ei:
        async for chunk in provider.chat_stream(req()):
            chunks.append(chunk)
    assert ei.value.kind == expected_kind, (
        f"{provider_name} classified streaming fault {fault!r} as {ei.value.kind!r}, "
        f"every adapter must agree on {expected_kind!r}"
    )
    assert ei.value.retryable is expected_retryable
    if fault != "error_before_commit":
        # ADR 0153 d9: once the first byte is out, it must never be discarded — a failed-over
        # retry would otherwise show the caller "partial" twice.
        assert chunks and chunks[0].text == "partial"
    else:
        assert chunks == []  # nothing committed before the error


def test_every_streaming_provider_is_actually_exercised():
    assert set(STREAM_PROVIDERS) == {"ollama", "openai_compat"}


async def test_the_streaming_fixtures_raise_on_an_unhandled_fault_name():
    """The fault name is only checked inside the mock transport's handler, which fires when a
    request is actually made — constructing the provider alone proves nothing; the assertion must
    drive an actual `chat_stream()` call to reach it."""
    for factory in STREAM_PROVIDERS.values():
        provider = factory("no-such-fault")
        with pytest.raises(AssertionError, match="unhandled streaming fault"):
            async for _ in provider.chat_stream(req()):
                pass


# ── embed conformance (design spec §12.2's "embed" dimension) ──────────────────────
#
# `.embed()` shares the exact same `_from_response`/`_from_exception` classification methods
# `.chat()` uses, and the `PROVIDERS` fault injectors above never look at which endpoint was hit —
# so the identical fault set applies unchanged here, calling `.embed()` instead of `.chat()`. This
# is the exercise that found a real bug (fixed 2026-09-25, same session): neither adapter's
# `.embed()` caught a malformed response body the way `.chat()` already did, so a raw
# `json.JSONDecodeError` — not a `ProviderError` — would escape a direct `.embed()` call, violating
# the `Provider` protocol's own documented contract ("one upstream failure into one
# `ProviderError`"). `GatewayCore` happened to catch and rewrap it anyway, so the gap was invisible
# through the routing core — exactly the kind of drift a per-adapter test file, or a test that only
# exercises providers *through* `GatewayCore`, cannot see.


@pytest.mark.parametrize("provider_name", list(PROVIDERS))
@pytest.mark.parametrize(
    ("fault", "expected_kind", "expected_retryable"),
    [
        ("connect_refused", "upstream_unavailable", True),
        ("timeout", "upstream_timeout", True),
        ("malformed_body", "upstream_error", True),
        ("429_retry_after", "rate_limited", True),
        ("5xx", "upstream_error", True),
        ("model_not_found", "model_not_found", False),
    ],
)
async def test_every_providers_embed_classifies_the_same_fault_as_its_own_chat_does(
    provider_name, fault, expected_kind, expected_retryable
):
    provider = PROVIDERS[provider_name](fault)
    with pytest.raises(ProviderError) as ei:
        await provider.embed("m", ["hello"])
    assert ei.value.kind == expected_kind, (
        f"{provider_name}.embed() classified {fault!r} as {ei.value.kind!r}, but "
        f"{provider_name}.chat() already agreed on {expected_kind!r} for the identical fault"
    )
    assert ei.value.retryable is expected_retryable


def _ollama_embed_ok() -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 3})

    return OllamaProvider("n1", "http://ollama.test", transport=httpx.MockTransport(handler))


def _openai_compat_embed_ok() -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m",
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 3},
            },
        )

    return OpenAICompatProvider(
        "router", "http://router.test", transport=httpx.MockTransport(handler)
    )


EMBED_OK_PROVIDERS: dict[str, Callable[[], Provider]] = {
    "ollama": _ollama_embed_ok,
    "openai_compat": _openai_compat_embed_ok,
}


@pytest.mark.parametrize("provider_name", list(EMBED_OK_PROVIDERS))
async def test_every_providers_embed_returns_the_same_result_shape_on_success(provider_name):
    provider = EMBED_OK_PROVIDERS[provider_name]()
    result = await provider.embed("m", ["hello"])
    assert result.vectors == [[0.1, 0.2]]
    assert result.provider == provider.name
    assert result.usage.prompt_tokens == 3
    assert result.usage.estimated is False


def test_every_embed_success_provider_is_actually_exercised():
    assert set(EMBED_OK_PROVIDERS) == {"ollama", "openai_compat"}


# ── tool-call conformance (design spec §12.2's "tool calls" dimension) ─────────────
#
# Found via this exact exercise (2026-09-25, same session as the embed fix above): the
# openai_compat adapter used to leave a tool call's `id` as `""` when the upstream omitted one,
# while the Ollama adapter always synthesises a unique one — a caller correlating several tool
# calls in one response to their results in the *next* request cannot do so when every id is the
# same empty string. Fixed to match; this dimension pins the agreement down so it cannot silently
# regress in either adapter alone.


def _ollama_tool_call_no_id() -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "lookup", "arguments": {"q": "x"}}}],
                },
                "done": True,
                "done_reason": "stop",
            },
        )

    return OllamaProvider("n1", "http://ollama.test", transport=httpx.MockTransport(handler))


def _openai_compat_tool_call_no_id() -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    return OpenAICompatProvider(
        "router", "http://router.test", transport=httpx.MockTransport(handler)
    )


TOOL_CALL_PROVIDERS: dict[str, Callable[[], Provider]] = {
    "ollama": _ollama_tool_call_no_id,
    "openai_compat": _openai_compat_tool_call_no_id,
}


@pytest.mark.parametrize("provider_name", list(TOOL_CALL_PROVIDERS))
async def test_every_provider_synthesises_a_tool_call_id_when_the_upstream_omits_one(
    provider_name,
):
    provider = TOOL_CALL_PROVIDERS[provider_name]()
    result = await provider.chat(req(tools=[{"type": "function", "function": {"name": "lookup"}}]))
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call["id"], f"{provider_name} left the tool call id empty when the upstream omitted one"
    assert call["function"]["name"] == "lookup"
    assert result.finish_reason == "tool_calls"


def test_every_tool_call_provider_is_actually_exercised():
    assert set(TOOL_CALL_PROVIDERS) == {"ollama", "openai_compat"}

"""OpenAI-compatible provider (ADR 0152 d3, PLAN.md P5) — driven against a fake router upstream.

Unlike the Ollama adapter, `ChatRequest` is already in the wire shape this provider speaks, so
most of these tests are about **passthrough fidelity** (nothing silently dropped or mangled) and
**error/quirk classification** (PLAN.md's "vendor-quirk table": usage-in-last-chunk, 429 shapes,
non-standard error envelopes) rather than translation, which is the Ollama adapter's job.
"""

from __future__ import annotations

import json

import httpx
import pytest

from examlops.gateway.egress import EgressDenied
from examlops.gateway.providers.base import ChatRequest, ProviderError
from examlops.gateway.providers.openai_compat import OpenAICompatProvider, OpenAICompatQuirks

BASE = "http://router.test:20128/v1"


def _sse(*events: dict | str) -> bytes:
    lines = []
    for e in events:
        lines.append(f"data: {e}\n\n" if isinstance(e, str) else f"data: {json.dumps(e)}\n\n")
    return "".join(lines).encode()


class FakeRouter:
    """A recording fake OpenAI-compatible upstream. ``chat``/``stream_body`` are replaceable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []  # (path, body, headers)
        self.models = {"data": [{"id": "gpt-judge", "object": "model"}, {"id": "auto"}]}
        self.chat = self._ok_chat
        self.stream_body = self._ok_stream

    @staticmethod
    def _ok_chat(body: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
        )

    @staticmethod
    def _ok_stream(body: dict) -> bytes:
        return _sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
            "[DONE]",
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        headers = dict(request.headers)
        path = request.url.path
        self.calls.append((path, body, headers))
        if path == "/v1/chat/completions":
            if body.get("stream"):
                return httpx.Response(
                    200,
                    content=self.stream_body(body),
                    headers={"content-type": "text/event-stream"},
                )
            return self.chat(body)
        if path == "/v1/models":
            return httpx.Response(200, json=self.models)
        if path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                    "usage": {"prompt_tokens": 4},
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})


@pytest.fixture
def fake() -> FakeRouter:
    return FakeRouter()


def make(fake: FakeRouter, **kw) -> OpenAICompatProvider:
    return OpenAICompatProvider("router", BASE, transport=httpx.MockTransport(fake), **kw)


def req(**kw) -> ChatRequest:
    kw.setdefault("model", "auto")
    kw.setdefault("messages", [{"role": "user", "content": "hi"}])
    return ChatRequest(**kw)


# ── chat: passthrough fidelity ─────────────────────────────────────────────────


async def test_chat_messages_and_params_are_passed_through_unmodified(fake):
    """Contrast with the Ollama adapter: nothing here is translated, only forwarded."""
    await make(fake).chat(
        req(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            temperature=0.3,
            top_p=0.9,
            max_tokens=64,
            stop=["END"],
            seed=7,
            tools=[{"type": "function", "function": {"name": "f"}}],
            tool_choice="auto",
            response_format={"type": "json_schema", "json_schema": {"name": "r", "schema": {}}},
        )
    )
    _, body, _ = fake.calls[0]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert body["temperature"] == 0.3 and body["top_p"] == 0.9 and body["max_tokens"] == 64
    assert body["stop"] == ["END"] and body["seed"] == 7
    assert body["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert body["tool_choice"] == "auto"
    assert body["response_format"]["type"] == "json_schema"


async def test_chat_reports_the_upstreams_own_model_not_the_requested_one(fake):
    """A router's `auto` alias may resolve to a concrete model — passed through as reported,
    never re-derived (module docstring: "an unverified claim, not ExaMLOps's own accounting")."""
    fake.chat = lambda b: httpx.Response(
        200,
        json={
            "model": "gpt-judge-mini",
            "choices": [{"index": 0, "message": {"content": "x"}, "finish_reason": "stop"}],
        },
    )
    result = await make(fake).chat(req(model="auto"))
    assert result.model == "gpt-judge-mini"


async def test_chat_normalises_tool_calls_and_usage(fake):
    fake.chat = lambda b: httpx.Response(
        200,
        json={
            "model": b["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2},
        },
    )
    result = await make(fake).chat(req())
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q": "x"}'},
        }
    ]
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (9, 2)
    assert result.usage.estimated is False


async def test_chat_synthesises_a_tool_call_id_when_the_router_omits_one(fake):
    """Matches the Ollama adapter's own synthetic-ID fallback — a lenient/non-compliant router
    that omits `id` (real OpenAI always sends one) must not make every returned tool call
    indistinguishable to a caller trying to correlate several of them to their results."""
    fake.chat = lambda b: httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"type": "function", "function": {"name": "a", "arguments": "{}"}},
                            {"type": "function", "function": {"name": "b", "arguments": "{}"}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    result = await make(fake).chat(req())
    ids = [c["id"] for c in result.tool_calls]
    assert all(ids) and len(set(ids)) == 2  # non-empty and distinct, not both ""


async def test_chat_marks_usage_estimated_when_upstream_omits_it(fake):
    fake.chat = lambda b: httpx.Response(
        200, json={"choices": [{"index": 0, "message": {"content": "x"}, "finish_reason": "stop"}]}
    )
    result = await make(fake).chat(req())
    assert result.usage.estimated is True


async def test_auth_header_is_sent_when_an_api_key_is_configured(fake):
    await make(fake, api_key="sk-test-123").chat(req())
    _, _, headers = fake.calls[0]
    assert headers.get("authorization") == "Bearer sk-test-123"


async def test_no_api_key_omits_the_authorization_header(fake):
    await make(fake).chat(req())
    _, _, headers = fake.calls[0]
    assert "authorization" not in headers


# ── streaming ──────────────────────────────────────────────────────────────────


async def test_stream_yields_content_then_a_final_usage_chunk(fake):
    """The fixture's first event is a role-only delta (empty text, real-world OpenAI behaviour) —
    `ttft_ms` marks the first chunk that carries actual content, not literally chunk zero."""
    chunks = [c async for c in make(fake).chat_stream(req())]
    text = "".join(c.text for c in chunks)
    assert text == "hi"
    assert chunks[0].ttft_ms is None  # role-only delta: no content yet
    content_chunk = next(c for c in chunks if c.text)
    assert content_chunk.ttft_ms is not None
    assert chunks[-2].finish_reason == "stop"  # the finish_reason chunk precedes the usage-only one
    assert chunks[-1].usage is not None
    assert (chunks[-1].usage.prompt_tokens, chunks[-1].usage.completion_tokens) == (5, 1)


async def test_stream_requests_stream_options_by_default(fake):
    await anext(make(fake).chat_stream(req()), None)
    async for _ in make(fake).chat_stream(req()):
        pass
    _, body, _ = fake.calls[-1]
    assert body["stream_options"] == {"include_usage": True}


async def test_stream_options_quirk_omits_the_field_when_disabled(fake):
    p = make(fake, quirks=OpenAICompatQuirks(send_stream_options=False))
    async for _ in p.chat_stream(req()):
        pass
    _, body, _ = fake.calls[-1]
    assert "stream_options" not in body


async def test_stream_error_event_after_first_chunk_is_not_retryable(fake):
    def broken(body):
        return _sse(
            {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
            {"error": {"message": "upstream died mid-stream"}},
        )

    fake.stream_body = broken
    chunks = []
    with pytest.raises(ProviderError) as ei:
        async for c in make(fake).chat_stream(req()):
            chunks.append(c)
    assert chunks and chunks[0].text == "partial"
    assert ei.value.kind == "stream_interrupted"
    assert ei.value.retryable is False


async def test_stream_error_event_before_any_content_is_retryable(fake):
    fake.stream_body = lambda b: _sse({"error": {"message": "refused"}})
    with pytest.raises(ProviderError) as ei:
        async for _ in make(fake).chat_stream(req()):
            pass
    assert ei.value.kind == "upstream_error" and ei.value.retryable is True


async def test_stream_malformed_json_event_is_classified(fake):
    fake.stream_body = lambda b: b"data: not json\n\n"
    with pytest.raises(ProviderError) as ei:
        async for _ in make(fake).chat_stream(req()):
            pass
    assert ei.value.kind == "upstream_error"


async def test_stream_tool_call_deltas_are_normalised(fake):
    fake.stream_body = lambda b: _sse(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "call_x",
                                "type": "function",
                                "function": {"name": "f", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    chunks = [c async for c in make(fake).chat_stream(req())]
    assert chunks[0].tool_calls[0]["function"]["name"] == "f"
    assert chunks[-1].finish_reason == "tool_calls"


async def test_sse_comment_and_blank_lines_are_ignored(fake):
    fake.stream_body = lambda b: (
        b": keep-alive\n\n"
        + _sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]})
        + b"\n"
        + _sse("[DONE]")
    )
    chunks = [c async for c in make(fake).chat_stream(req())]
    assert "".join(c.text for c in chunks) == "ok"


# ── errors (ADR 0153 d4 classification, ADR 0156 d1 codes) ────────────────────


@pytest.mark.parametrize(
    ("status", "body", "kind", "retryable"),
    [
        (400, {"error": {"message": "bad"}}, "invalid_request", False),
        (401, {"error": {"message": "no key"}}, "upstream_error", False),
        (403, {"error": {"message": "forbidden"}}, "upstream_error", False),
        (404, {"error": {"message": "no such model"}}, "model_not_found", False),
        (500, {"error": {"message": "boom"}}, "upstream_error", True),
        (503, {"error": {"message": "busy"}}, "upstream_error", True),
    ],
)
async def test_http_errors_are_classified(fake, status, body, kind, retryable):
    fake.chat = lambda b: httpx.Response(status, json=body)
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert (ei.value.kind, ei.value.retryable) == (kind, retryable)
    assert ei.value.provider == "router"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": {"message": "wrapped"}}, "wrapped"),
        ({"error": "bare string error"}, "bare string error"),
        ({"message": "top-level message"}, "top-level message"),
        ({"detail": "FastAPI-style detail (OmniRoute is FastAPI-based)"}, "FastAPI-style detail"),
    ],
)
async def test_error_envelope_shapes_all_extract_a_message(fake, body, expected):
    """Each shape's own distinctive text must appear — a check for "any non-empty string" would
    pass even with extraction fully broken, because `_from_response`'s `resp.text` fallback (for a
    server that sends no recognised shape at all) makes the raw JSON dump the message instead, and
    that's also non-empty. Asserting `not startswith("{")` catches exactly that fallback path."""
    fake.chat = lambda b: httpx.Response(400, json=body)
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert expected in ei.value.message
    assert not ei.value.message.startswith("{")


async def test_429_retry_after_from_header(fake):
    fake.chat = lambda b: httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow"})
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert ei.value.retry_after == 3.0


async def test_429_retry_after_from_body_when_no_header(fake):
    fake.chat = lambda b: httpx.Response(429, json={"error": {"message": "slow", "retry_after": 2}})
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert ei.value.retry_after == 2.0


async def test_429_retry_after_quirk_treats_body_value_as_milliseconds(fake):
    fake.chat = lambda b: httpx.Response(
        429, json={"error": {"message": "slow", "retry_after": 500}}
    )
    p = make(fake, quirks=OpenAICompatQuirks(retry_after_is_ms=True))
    with pytest.raises(ProviderError) as ei:
        await p.chat(req())
    assert ei.value.retry_after == 0.5


async def test_connect_failure_is_upstream_unavailable(fake):
    def boom(body):
        raise httpx.ConnectError("refused")

    fake.chat = boom
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert (ei.value.kind, ei.value.retryable) == ("upstream_unavailable", True)


async def test_malformed_response_body_raises_upstream_error(fake):
    fake.chat = lambda b: httpx.Response(200, text="not json")
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert ei.value.kind == "upstream_error"


# ── discovery, probe, embeddings ────────────────────────────────────────────────


async def test_list_models_from_the_openai_models_endpoint(fake):
    names = {m.name for m in await make(fake).list_models()}
    assert names == {"gpt-judge", "auto"}


async def test_probe_reports_ok(fake):
    res = await make(fake).probe()
    assert res.ok is True and res.latency_ms >= 0


async def test_probe_never_raises_on_an_unreachable_upstream():
    def refuse(request):
        raise httpx.ConnectError("refused")

    p = OpenAICompatProvider("router", BASE, transport=httpx.MockTransport(refuse))
    res = await p.probe()
    assert res.ok is False and "refused" in res.detail


async def test_embed(fake):
    res = await make(fake).embed("embed-model", ["hello"])
    assert res.vectors == [[0.1, 0.2]]
    assert res.usage.prompt_tokens == 4
    assert res.usage.estimated is False


async def test_embed_with_a_malformed_response_raises_a_typed_provider_error(fake):
    """Matches `chat()`'s own malformed-body handling — a `Provider.embed()` that let a raw
    `JSONDecodeError` escape instead would violate the documented protocol contract, even though
    `GatewayCore` happens to catch and rewrap any exception anyway; a provider used directly
    (bypassing the routing core) must not leak one."""

    def malformed_embed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/embeddings":
            return httpx.Response(200, text="not json")
        return fake(request)

    p = OpenAICompatProvider("router", BASE, transport=httpx.MockTransport(malformed_embed))
    with pytest.raises(ProviderError) as ei:
        await p.embed("embed-model", ["hello"])
    assert ei.value.kind == "upstream_error"


# ── egress (ADR 0154) ────────────────────────────────────────────────────────


def test_provider_construction_applies_the_egress_check():
    with pytest.raises(EgressDenied):
        OpenAICompatProvider("bad", "https://acme.openai.azure.com/v1", locality="external")


def test_construction_allows_a_declared_external_router():
    p = OpenAICompatProvider("openrouter", "https://openrouter.ai/api/v1", locality="external")
    assert p.base_url == "https://openrouter.ai/api/v1"


# ── constrains_schema is operator-set, never inferred ─────────────────────────


def test_constrains_schema_defaults_to_false():
    p = OpenAICompatProvider(
        "router", BASE, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert p.constrains_schema is False


def test_constrains_schema_can_be_enabled():
    p = OpenAICompatProvider(
        "vllm",
        BASE,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        constrains_schema=True,
    )
    assert p.constrains_schema is True

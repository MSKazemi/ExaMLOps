"""Ollama provider (ADR 0152) — driven against a fake upstream over ``httpx.MockTransport``.

The fake speaks the *native* Ollama API (``/api/chat``, ``/api/tags``, ``/api/ps``, ``/api/show``,
``/api/embed``) with the response shapes observed on a live Ollama 0.30.2, so a passing test means the
adapter handles what a real server sends — and every failure class (ADR 0153 d4) is injected here
rather than waiting for one to happen in production.
"""

from __future__ import annotations

import json

import httpx
import pytest

from examlops.gateway import Completion, GatewayClient, Router
from examlops.gateway.egress import EgressDenied, check_resolved_addresses, validate_base_url
from examlops.gateway.providers import (
    ChatRequest,
    OllamaProvider,
    ProviderError,
    provider_backend,
)

BASE = "http://ollama.test:11434"


def _ndjson(*objs: dict) -> bytes:
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()


class FakeOllama:
    """A recording fake. ``chat`` is replaced per test to inject behaviour."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.tags = {
            "models": [
                {
                    "name": "qwen3:8b",
                    "size": 5,
                    "capabilities": ["completion", "tools", "thinking"],
                },
                {"name": "nomic-embed-text:latest", "size": 1, "capabilities": ["embedding"]},
                {"name": "old:1b", "size": 2},  # no capabilities field: adapter must ask /api/show
            ]
        }
        self.ps = {"models": [{"name": "qwen3:8b"}]}
        self.show = {"capabilities": ["completion"], "model_info": {"llama.context_length": 8192}}
        self.chat = self._ok_chat

    @staticmethod
    def _ok_chat(body: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": "hello"},
                "done": True,
                "done_reason": "stop",
                "load_duration": 3_000_000_000,
                "total_duration": 3_500_000_000,
                "prompt_eval_count": 26,
                "eval_count": 8,
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.url.path, body))
        path = request.url.path
        if path == "/api/chat":
            return self.chat(body)
        if path == "/api/tags":
            return httpx.Response(200, json=self.tags)
        if path == "/api/ps":
            return httpx.Response(200, json=self.ps)
        if path == "/api/show":
            return httpx.Response(200, json=self.show)
        if path == "/api/embed":
            return httpx.Response(
                200,
                json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 3, "model": body["model"]},
            )
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fake() -> FakeOllama:
    return FakeOllama()


def make(fake: FakeOllama, **kw) -> OllamaProvider:
    return OllamaProvider("ollama-n1", BASE, transport=httpx.MockTransport(fake), **kw)


def req(**kw) -> ChatRequest:
    kw.setdefault("model", "qwen3:8b")
    kw.setdefault("messages", [{"role": "user", "content": "hi"}])
    return ChatRequest(**kw)


# ── chat ──────────────────────────────────────────────────────────────────────


async def test_chat_maps_openai_params_to_native_options(fake):
    p = make(fake, keep_alive="30m", options={"num_ctx": 16384})
    res = await p.chat(
        req(temperature=0.2, top_p=0.9, max_tokens=64, stop=["x"], seed=7, extra={"think": False})
    )
    body = dict(fake.calls)["/api/chat"]
    assert body["stream"] is False
    assert body["keep_alive"] == "30m"
    assert body["think"] is False
    assert body["options"] == {
        "num_ctx": 16384,
        "temperature": 0.2,
        "top_p": 0.9,
        "num_predict": 64,
        "stop": ["x"],
        "seed": 7,
    }
    assert res.text == "hello"
    assert res.finish_reason == "stop"
    assert (res.usage.prompt_tokens, res.usage.completion_tokens) == (26, 8)
    assert res.usage.estimated is False
    assert res.load_ms == pytest.approx(3000.0)
    assert res.provider == "ollama-n1"


async def test_request_extra_overrides_deployment_defaults(fake):
    p = make(fake, options={"num_ctx": 16384})
    await p.chat(req(extra={"num_ctx": 4096, "keep_alive": "5m"}))
    body = dict(fake.calls)["/api/chat"]
    assert body["options"]["num_ctx"] == 4096
    assert body["keep_alive"] == "5m"


async def test_length_finish_reason_is_normalised(fake):
    fake.chat = lambda b: httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": "x"},
            "done": True,
            "done_reason": "length",
        },
    )
    assert (await make(fake).chat(req())).finish_reason == "length"


async def test_reasoning_is_kept_out_of_content_and_tool_calls_are_normalised(fake):
    fake.chat = lambda b: httpx.Response(
        200,
        json={
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "let me think",
                "tool_calls": [
                    {"function": {"name": "get_status", "arguments": {"model": "JPCP"}}}
                ],
            },
            "done": True,
            "done_reason": "stop",
        },
    )
    res = await make(fake).chat(
        req(tools=[{"type": "function", "function": {"name": "get_status"}}])
    )
    assert res.text == ""
    assert res.reasoning == "let me think"
    assert res.finish_reason == "tool_calls"
    call = res.tool_calls[0]
    assert call["type"] == "function" and call["id"]
    assert call["function"]["name"] == "get_status"
    assert json.loads(call["function"]["arguments"]) == {"model": "JPCP"}
    assert dict(fake.calls)["/api/chat"]["tools"]


async def test_json_schema_response_format_becomes_native_format(fake):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    await make(fake).chat(
        req(response_format={"type": "json_schema", "json_schema": {"name": "s", "schema": schema}})
    )
    assert dict(fake.calls)["/api/chat"]["format"] == schema


async def test_content_parts_are_flattened_and_images_extracted(fake):
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
        }
    ]
    await make(fake).chat(req(messages=msgs))
    sent = dict(fake.calls)["/api/chat"]["messages"][0]
    assert sent["content"] == "what is this"
    assert sent["images"] == ["QUJD"]


# ── streaming ─────────────────────────────────────────────────────────────────


async def test_stream_yields_chunks_then_usage(fake):
    fake.chat = lambda b: httpx.Response(
        200,
        content=_ndjson(
            {"message": {"role": "assistant", "content": "he"}, "done": False},
            {"message": {"role": "assistant", "content": "llo"}, "done": False},
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 2,
                "load_duration": 1_000_000_000,
            },
        ),
    )
    chunks = [c async for c in make(fake).chat_stream(req())]
    assert "".join(c.text for c in chunks) == "hello"
    last = chunks[-1]
    assert last.finish_reason == "stop"
    assert (last.usage.prompt_tokens, last.usage.completion_tokens) == (5, 2)
    assert dict(fake.calls)["/api/chat"]["stream"] is True


async def test_stream_error_after_first_chunk_is_not_retryable(fake):
    fake.chat = lambda b: httpx.Response(
        200,
        content=_ndjson(
            {"message": {"role": "assistant", "content": "he"}, "done": False},
            {"error": "llama runner process has terminated"},
        ),
    )
    seen: list[str] = []
    with pytest.raises(ProviderError) as ei:
        async for c in make(fake).chat_stream(req()):
            seen.append(c.text)
    assert seen == ["he"]
    assert ei.value.kind == "stream_interrupted"
    assert ei.value.retryable is False  # ADR 0153 d9: never fail over after the first byte


# ── error classification (ADR 0153 d4) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("make_response", "kind", "retryable"),
    [
        (
            lambda b: httpx.Response(404, json={"error": "model 'zzz' not found"}),
            "model_not_found",
            False,
        ),
        (lambda b: httpx.Response(400, json={"error": "bad"}), "invalid_request", False),
        (lambda b: httpx.Response(500, json={"error": "boom"}), "upstream_error", True),
        (lambda b: httpx.Response(503, text="busy"), "upstream_error", True),
        (lambda b: httpx.Response(200, text="not json"), "upstream_error", True),
    ],
)
async def test_http_errors_are_classified(fake, make_response, kind, retryable):
    fake.chat = make_response
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert ei.value.kind == kind
    assert ei.value.retryable is retryable
    assert ei.value.provider == "ollama-n1"


async def test_429_carries_retry_after(fake):
    fake.chat = lambda b: httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow"})
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert ei.value.kind == "rate_limited"
    assert ei.value.retry_after == 7.0
    assert ei.value.retryable is True


async def test_connect_failure_is_upstream_unavailable(fake):
    def boom(body):
        raise httpx.ConnectError("refused")

    fake.chat = boom
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert (ei.value.kind, ei.value.retryable) == ("upstream_unavailable", True)


async def test_read_timeout_is_upstream_timeout(fake):
    def slow(body):
        raise httpx.ReadTimeout("slow")

    fake.chat = slow
    with pytest.raises(ProviderError) as ei:
        await make(fake).chat(req())
    assert (ei.value.kind, ei.value.retryable) == ("upstream_timeout", True)


def test_error_kinds_map_to_the_documented_http_status():
    table = {
        "invalid_request": 400,
        "model_not_found": 404,
        "rate_limited": 429,
        "upstream_unavailable": 503,
        "upstream_timeout": 504,
        "upstream_error": 502,
    }
    for kind, status in table.items():
        assert ProviderError(kind, "x").http_status == status


# ── discovery, residency, probe, embeddings ───────────────────────────────────


async def test_list_models_reads_capabilities_and_falls_back_to_show(fake):
    models = {m.name: m for m in await make(fake).list_models()}
    assert models["qwen3:8b"].capabilities.tools and models["qwen3:8b"].capabilities.thinking
    assert models["qwen3:8b"].resident is True
    assert models["nomic-embed-text:latest"].capabilities.embeddings is True
    assert models["nomic-embed-text:latest"].capabilities.chat is False
    # `old:1b` had no capabilities in /api/tags → /api/show was consulted, incl. context window
    assert models["old:1b"].capabilities.chat is True
    assert models["old:1b"].capabilities.context_window == 8192
    assert models["old:1b"].resident is False
    assert "/api/show" in [path for path, _ in fake.calls]


async def test_probe_reports_ok_and_resident_models(fake):
    res = await make(fake).probe()
    assert res.ok is True and res.resident == ["qwen3:8b"] and res.latency_ms >= 0


async def test_probe_never_raises_on_an_unreachable_upstream(fake):
    def refuse(request):
        raise httpx.ConnectError("refused")

    p = OllamaProvider("ollama-n1", BASE, transport=httpx.MockTransport(refuse))
    res = await p.probe()
    assert res.ok is False
    assert "refused" in res.detail


async def test_embed(fake):
    res = await make(fake).embed("nomic-embed-text:latest", ["hello"])
    assert res.vectors == [[0.1, 0.2]]
    assert res.usage.prompt_tokens == 3


async def test_embed_with_a_malformed_response_raises_a_typed_provider_error(fake):
    """Matches `chat()`'s own malformed-body handling — a `Provider.embed()` that let a raw
    `JSONDecodeError` escape instead would violate the documented protocol contract ("one upstream
    failure into one `ProviderError`"), even though `GatewayCore` happens to catch and rewrap any
    exception anyway; a provider used directly (bypassing the routing core) must not leak one."""

    def malformed_embed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            return httpx.Response(200, text="not json")
        return fake(request)

    p = OllamaProvider("ollama-n1", BASE, transport=httpx.MockTransport(malformed_embed))
    with pytest.raises(ProviderError) as ei:
        await p.embed("nomic-embed-text:latest", ["hello"])
    assert ei.value.kind == "upstream_error"


# ── egress (ADR 0154 d2/d5) ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.openai.azure.com/openai/v1",
        "https://x.cognitiveservices.azure.com",
        "https://gw.azure-api.net/llm",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/",
        "ftp://host/",
        "http://user:pass@host:11434",
    ],
)
def test_egress_refuses_forbidden_targets(url):
    with pytest.raises(EgressDenied):
        validate_base_url(url, locality="local")


def test_egress_allows_local_endpoints_for_local_providers():
    for url in (
        "http://127.0.0.1:11434",
        "http://host.docker.internal:11436",
        "http://ollama:11434",
    ):
        assert validate_base_url(url, locality="local") == url.rstrip("/")


def test_egress_refuses_private_addresses_for_external_providers_unless_allowed():
    with pytest.raises(EgressDenied):
        validate_base_url("http://10.1.2.3:20128/v1", locality="external")
    assert validate_base_url(
        "http://10.1.2.3:20128/v1", locality="external", allowed_hosts=("10.1.2.3",)
    )


def test_provider_construction_applies_the_egress_check():
    with pytest.raises(EgressDenied):
        OllamaProvider("bad", "https://acme.openai.azure.com")


# ── DNS-rebinding-safe egress: the resolved-address check (BL-111, 2026-09-24) ────────────────


def _resolver(*addrs: str):
    """A fake `socket.getaddrinfo`: returns one (family, type, proto, canon, sockaddr) per addr.

    Accepts both call conventions used in this file — ``check_resolved_addresses``'s
    ``(host, None, family=, type=)`` and ``_resolve_approved_address``'s ``(host, port, proto=)`` —
    so one fake serves both the pre-flight-check tests and the guarded-connection tests below.
    """

    def _resolve(host, port=None, **_kw):
        return [(0, 0, 0, "", (a, port or 0)) for a in addrs]

    return _resolve


def test_resolved_address_check_refuses_a_rebound_private_address():
    """A hostname the operator declared external, but which now resolves to an internal address —
    the exact DNS-rebinding shape: the string check at construction time cannot see this."""
    with pytest.raises(EgressDenied, match="platform-internal or private"):
        check_resolved_addresses(
            "https://router.example.org/v1",
            locality="external",
            resolver=_resolver("10.0.0.5"),
        )


def test_resolved_address_check_refuses_a_metadata_endpoint():
    with pytest.raises(EgressDenied, match="metadata"):
        check_resolved_addresses(
            "http://sneaky.example.com", locality="local", resolver=_resolver("169.254.169.254")
        )


def test_resolved_address_check_allows_a_legitimate_external_address():
    result = check_resolved_addresses(
        "https://router.example.org/v1", locality="external", resolver=_resolver("8.8.8.8")
    )
    assert result is None  # completes and returns — its only contract is "raise, or don't"


def test_resolved_address_check_allows_an_explicitly_permitted_host_even_if_private():
    result = check_resolved_addresses(  # the operator named this host on purpose
        "https://router.example.org/v1",
        locality="external",
        allowed_hosts=("router.example.org",),
        resolver=_resolver("10.0.0.5"),
    )
    assert result is None


def test_resolved_address_check_is_a_noop_for_a_literal_ip():
    calls = []
    result = check_resolved_addresses(
        "http://10.0.0.5:8000", locality="external", resolver=lambda *a, **kw: calls.append(1)
    )
    assert result is None
    assert calls == []  # the resolver is never invoked for a literal IP


def test_resolved_address_check_is_a_noop_on_resolution_failure():
    def _fails(*a, **kw):
        raise OSError("name resolution failed")

    check_resolved_addresses("http://unreachable.test", locality="local", resolver=_fails)


def test_resolved_address_check_examines_every_returned_address():
    """One good answer must not shadow a bad one — every resolved address is checked."""
    with pytest.raises(EgressDenied):
        check_resolved_addresses(
            "https://router.example.org/v1",
            locality="external",
            resolver=_resolver("8.8.8.8", "10.0.0.5"),
        )


async def test_the_ollama_provider_actually_calls_the_resolved_address_check(monkeypatch, fake):
    """Proves the wiring, not just the standalone function — patches the binding `ollama.py`
    itself holds (`from ... import check_resolved_addresses`), not the one in `egress` (a name
    imported with `from X import Y` is a separate reference from `X.Y` after that point)."""
    import examlops.gateway.providers.ollama as ollama_mod

    calls = []

    def _spy(url, **kw):
        calls.append((url, kw))

    monkeypatch.setattr(ollama_mod, "check_resolved_addresses", _spy)
    provider = make(fake, locality="local")
    await provider.chat(req())
    assert calls and calls[0][0] == BASE


async def test_a_rebound_address_blocks_the_request_before_it_reaches_the_upstream(
    monkeypatch, fake
):
    import examlops.gateway.providers.ollama as ollama_mod

    def _deny(*a, **kw):
        raise EgressDenied("rebound")

    monkeypatch.setattr(ollama_mod, "check_resolved_addresses", _deny)
    provider = make(fake, locality="local")
    with pytest.raises(EgressDenied):
        await provider.chat(req())
    assert fake.calls == []  # never reached the upstream


# ── the guarded backend: real TCP connections pinned, not just pre-flight checked ─────────────


class _StubStream:
    async def aclose(self):
        pass


class _StubInner:
    """A minimal `httpcore.AsyncNetworkBackend` recording exactly the address it was asked to
    connect to — proves the guarded backend hands it the *resolved* address, not the hostname."""

    def __init__(self):
        self.connected_to: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected_to.append((host, port))
        return _StubStream()


async def test_guarded_backend_connects_to_the_checked_address_not_the_hostname():
    from examlops.gateway.egress import AsyncGuardedBackend

    inner = _StubInner()
    backend = AsyncGuardedBackend(
        inner, locality="local", allowed_hosts=(), resolver=_resolver("10.9.8.7")
    )
    await backend.connect_tcp("ollama-n1", 11434)
    assert inner.connected_to == [("10.9.8.7", 11434)]  # the resolved IP, not "ollama-n1"


async def test_guarded_backend_refuses_a_rebound_address_before_ever_connecting():
    from examlops.gateway.egress import AsyncGuardedBackend

    inner = _StubInner()
    backend = AsyncGuardedBackend(
        inner, locality="external", allowed_hosts=(), resolver=_resolver("10.0.0.5")
    )
    with pytest.raises(EgressDenied):
        await backend.connect_tcp("router.example.org", 443)
    assert inner.connected_to == []


async def test_guarded_backend_refuses_a_unix_socket():
    from examlops.gateway.egress import AsyncGuardedBackend

    backend = AsyncGuardedBackend(_StubInner(), locality="local", allowed_hosts=())
    with pytest.raises(EgressDenied):
        await backend.connect_unix_socket("/tmp/whatever")


async def test_guarded_async_client_is_actually_wired():
    """If an httpx upgrade moves the private attributes, fail loudly instead of running unguarded —
    same discipline as `examlops.dataplane.safety.test_guarded_client_is_actually_wired`."""
    from examlops.gateway.egress import AsyncGuardedBackend, guarded_async_client

    client = guarded_async_client("http://ollama.test:11434", locality="local")
    try:
        assert isinstance(
            client._transport._pool._network_backend,  # type: ignore[attr-defined]
            AsyncGuardedBackend,
        )
        assert client._trust_env is False
        assert client.follow_redirects is False
    finally:
        await client.aclose()


async def test_guarded_async_client_denies_a_rebound_private_address_over_a_real_socket():
    """End-to-end: a real local server exists and is reachable, but the provider is declared
    external and the (faked) resolution points at a private address — the guard must refuse the
    connection before the real server ever sees a request."""
    import asyncio
    import http.server
    import threading

    from examlops.gateway.egress import guarded_async_client

    received: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        # A rebound resolver: the hostname "looks" external, but resolves to loopback — exactly
        # the real server's own address, proving the refusal is about the *address*, not
        # reachability (a real, live, reachable server is what gets refused here).
        client = guarded_async_client(
            "https://router.example.org",
            locality="external",
            timeout=2.0,
            resolver=_resolver("127.0.0.1"),
        )
        try:
            with pytest.raises(Exception):  # EgressDenied, surfaced through httpx's connect path
                await client.get(f"http://router.example.org:{port}/")
        finally:
            await client.aclose()
    finally:
        httpd.shutdown()
        await asyncio.to_thread(thread.join, 2)
        httpd.server_close()
    assert received == []  # the real server never saw the request


async def test_guarded_async_client_allows_an_explicitly_permitted_loopback_target():
    import asyncio
    import http.server
    import threading

    from examlops.gateway.egress import guarded_async_client

    received: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        client = guarded_async_client(
            f"http://onsite.example.org:{port}",
            locality="external",
            allowed_hosts=("onsite.example.org",),
            timeout=2.0,
            resolver=_resolver("127.0.0.1"),
        )
        try:
            resp = await client.get(f"http://onsite.example.org:{port}/probe")
        finally:
            await client.aclose()
    finally:
        httpd.shutdown()
        await asyncio.to_thread(thread.join, 2)
        httpd.server_close()
    assert resp.status_code == 200
    assert received == ["/probe"]


# ── the bridge into the existing sync gateway ─────────────────────────────────


def test_provider_backend_serves_the_existing_gateway_client(fake, monkeypatch):
    monkeypatch.setenv("GUARDRAIL_MODE", "off")
    router = Router()
    router.add_route("qwen3:8b", [("ollama-n1", provider_backend(make(fake)))])
    comp = GatewayClient(router, guardrail=None).chat(
        "qwen3:8b", [{"role": "user", "content": "hi"}]
    )
    assert isinstance(comp, Completion)
    assert comp.text == "hello" and comp.backend == "ollama-n1"
    assert (comp.prompt_tokens, comp.completion_tokens) == (26, 8)


def test_provider_backend_passes_a_response_schema_as_native_format(fake):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    fake.chat = lambda b: httpx.Response(
        200, json={"message": {"role": "assistant", "content": '{"a": 1}'}, "done": True}
    )
    backend = provider_backend(make(fake))
    assert backend.constrains_schema is True
    backend("qwen3:8b", [{"role": "user", "content": "x"}], response_schema=schema)
    assert dict(fake.calls)["/api/chat"]["format"] == schema


def test_provider_backend_health_reflects_the_probe(fake):
    assert provider_backend(make(fake)).health() is True

    def refuse(request):
        raise httpx.ConnectError("refused")

    down = OllamaProvider("d", BASE, transport=httpx.MockTransport(refuse))
    assert provider_backend(down).health() is False

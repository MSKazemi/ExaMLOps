"""Live Ollama provider check (ADR 0152) — opt-in, against a real Ollama server.

    EXAMLOPS_LIVE_OLLAMA=http://127.0.0.1:11434 pytest tests/integration/test_gateway_ollama_live.py -m live

Skips itself when the variable is unset. ``EXAMLOPS_LIVE_OLLAMA_MODEL`` picks the chat model
(default ``llama3.2:3b``, the cheapest one on n1).
"""

from __future__ import annotations

import os
import time

import pytest

from examlops.gateway import GatewayClient, Router
from examlops.gateway.providers import (
    ChatRequest,
    OllamaProvider,
    ProviderError,
    add_provider_routes,
)

URL = os.getenv("EXAMLOPS_LIVE_OLLAMA", "")
MODEL = os.getenv("EXAMLOPS_LIVE_OLLAMA_MODEL", "llama3.2:3b")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not URL, reason="set EXAMLOPS_LIVE_OLLAMA to a reachable Ollama base URL"),
]


def _msg(text: str = "Reply with the single word: ok") -> list[dict]:
    return [{"role": "user", "content": text}]


async def test_probe_and_discovery():
    p = OllamaProvider("live", URL)
    probe = await p.probe()
    assert probe.ok, probe.detail
    names = {m.name: m for m in await p.list_models()}
    assert MODEL in names and names[MODEL].capabilities.chat


async def test_chat_reports_real_usage_and_timing():
    res = await OllamaProvider("live", URL).chat(
        ChatRequest(MODEL, _msg(), max_tokens=8, temperature=0)
    )
    assert res.text.strip()
    assert res.usage.completion_tokens > 0 and res.usage.estimated is False
    assert res.load_ms is not None and res.total_ms and res.total_ms >= res.load_ms


async def test_stream_yields_incrementally_with_ttft_and_usage():
    chunks = [
        c
        async for c in OllamaProvider("live", URL).chat_stream(
            ChatRequest(MODEL, _msg("Count from 1 to 5"), max_tokens=32, temperature=0)
        )
    ]
    assert len(chunks) > 1, "a stream must arrive in more than one piece"
    assert next(c for c in chunks if c.ttft_ms is not None).ttft_ms > 0
    assert chunks[-1].finish_reason in ("stop", "length") and chunks[-1].usage.completion_tokens > 0


async def test_unknown_model_is_a_typed_non_retryable_error():
    with pytest.raises(ProviderError) as ei:
        await OllamaProvider("live", URL).chat(ChatRequest("no-such-model:1b", _msg()))
    assert ei.value.kind == "model_not_found" and ei.value.retryable is False


async def test_dead_endpoint_fails_fast_and_typed():
    dead = OllamaProvider("dead", "http://127.0.0.1:9")  # discard port: connection refused
    started = time.perf_counter()
    with pytest.raises(ProviderError) as ei:
        await dead.chat(ChatRequest(MODEL, _msg()))
    assert ei.value.kind == "upstream_unavailable" and ei.value.retryable is True
    assert time.perf_counter() - started < 5.0  # the connect timeout, not the 300 s read timeout
    assert (await dead.probe()).ok is False


def test_existing_sync_gateway_client_serves_through_the_provider():
    router = Router()
    added = add_provider_routes(router, OllamaProvider("live", URL), only={MODEL})
    assert added == [MODEL]
    comp = GatewayClient(router, guardrail=None).chat(MODEL, _msg(), max_tokens=8, temperature=0)
    assert comp.text.strip() and comp.backend == "live" and comp.completion_tokens > 0

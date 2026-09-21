"""Provider protocol and the normalised request/result types (ADR 0152).

A provider is a thin translator: it turns one normalised request into one upstream call and one
upstream failure into one :class:`ProviderError`. Routing, retry, breaker, budget, cache and
guardrails are never in a provider (ADR 0151 d2, 0153).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

#: ADR 0156 d1 / spec §6: the public error codes and the HTTP status each maps to.
ERROR_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "capability_unavailable": 400,
    "context_overflow": 400,
    "key_invalid": 401,
    "model_not_allowed": 403,
    "locality_denied": 403,
    "guardrail_blocked": 403,
    "model_not_found": 404,
    "budget_exceeded": 429,
    "rate_limited": 429,
    "queue_full": 429,
    "model_loading": 503,
    "upstream_unavailable": 503,
    "gateway_unavailable": 503,
    "upstream_timeout": 504,
    "upstream_error": 502,
    "stream_interrupted": 502,
    "config_invalid": 422,
    "internal_error": 500,
}
#: Failure classes worth another attempt (ADR 0153 d4). A client mistake never is.
_RETRYABLE = frozenset(
    {"rate_limited", "upstream_unavailable", "upstream_timeout", "upstream_error"}
)


class ProviderError(Exception):
    """One classified upstream failure. ``kind`` is a public error code (ADR 0156)."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after: float | None = None,
        status: int | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = kind in _RETRYABLE if retryable is None else retryable
        self.retry_after = retry_after
        self.status = status  # the *upstream's* HTTP status, when there was one
        self.provider = provider
        #: Every attempt made before this failure (provider, model, outcome, ms) — never prompt content.
        self.attempts: list[dict[str, Any]] = []

    @property
    def http_status(self) -> int:
        """The status the gateway itself answers with for this failure."""
        return ERROR_STATUS.get(self.kind, 502)

    def __str__(self) -> str:
        who = f"{self.provider}: " if self.provider else ""
        return f"{who}{self.kind}: {self.message}"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: True when the provider did not report counts and these are guesses — FinOps must not
    #: present an estimate as a measurement (ADR 0156 d6).
    estimated: bool = False


@dataclass
class Capabilities:
    chat: bool = True
    embeddings: bool = False
    tools: bool = False
    vision: bool = False
    thinking: bool = False
    json_schema: bool = True
    context_window: int | None = None


@dataclass
class ModelInfo:
    name: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    size_bytes: int | None = None
    resident: bool | None = None  # None = the provider cannot say


@dataclass
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    #: Provider-specific hints (``num_ctx``, ``keep_alive``, ``think``); ignored by providers that
    #: do not understand them, and always narrower than the deployment's own settings.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResult:
    text: str
    model: str
    provider: str
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"  # stop | length | tool_calls | content_filter | error
    usage: Usage = field(default_factory=Usage)
    ttft_ms: float | None = None
    load_ms: float | None = None
    total_ms: float | None = None


@dataclass
class ChatChunk:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None  # set only on the terminal chunk
    usage: Usage | None = None  # set only on the terminal chunk
    ttft_ms: float | None = None  # set only on the first chunk that carries content
    load_ms: float | None = None


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float
    detail: str = ""
    resident: list[str] = field(default_factory=list)


class Provider(Protocol):
    name: str
    type: str
    locality: str

    async def chat(self, req: ChatRequest) -> ChatResult: ...

    def chat_stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def probe(self) -> ProbeResult: ...


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a provider coroutine from synchronous code (the in-process ``GatewayClient``).

    Safe from inside a running event loop too: it then runs on a private loop in a worker thread
    instead of failing with "asyncio.run() cannot be called from a running event loop".
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

"""The LLM gateway service (ADR 0151): an OpenAI-compatible API in front of every model.

This module is the *edge*: authentication, request parsing, the OpenAI wire shapes, SSE framing, the
typed error envelope (ADR 0156 d1), readiness, admin and metrics. Which deployment serves a request,
and what happens when one fails, is :class:`examlops.gateway.routing.GatewayCore`; where the models
are is :mod:`examlops.gateway.config`. Keys, budgets and usage accounting reuse the gateway library
so the in-process client and this service can never disagree about them.

D8 guardrails (input + output scan), the B3 semantic cache and B1 prompt-registry references are
also reused from the gateway library here (2026-09-23) — the in-process
:class:`examlops.gateway.GatewayClient` and this network edge must not disagree about what is
scanned, cached or templated any more than they already agree about keys and budgets. The streaming
path gets the same input scan and prompt-ref resolution as the non-streaming one; output scanning
and the semantic cache are non-streaming only for now (see the docstrings on ``chat_completions``).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from examlops.gateway import (
    BudgetExceeded,
    GatewayError,
    GuardrailBlocked,
    KeyInvalid,
    ModelNotAllowed,
    _account,
    _cache_params,
    _cacheable,
    _estimate_cost,
    _guard_messages,
    _hash_key,
    _truthy,
    authorize,
    default_guardrail,
    resolve_prompt_ref,
)
from examlops.gateway.config import (
    ConfigError,
    ProviderFactory,
    Runtime,
    build_runtime,
    default_config_path,
    default_provider_factory,
    generated_config,
    load_config_file,
)
from examlops.gateway.providers import (
    ChatChunk,
    ChatRequest,
    ChatResult,
    ProbeResult,
    ProviderError,
    Usage,
)
from examlops.semantic_cache import SemanticCache, bind_to_gateway

logger = logging.getLogger(__name__)

_MAX_BODY = 8 * 1024 * 1024  # parity with the serving gateway (ADR 0126)
_WEAK_TOKENS = frozenset(
    {"changeme", "change-me", "changeme123", "password", "secret", "admin", "token"}
)
_RID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_BREAKER_VALUE = {"closed": 0, "half_open": 1, "open": 2}


class _ChatBody(BaseModel):
    """The OpenAI chat request. Unknown fields are ignored: real clients send many we do not use."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    examlops: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None


class _Http(Exception):
    """A failure the edge itself detects before any routing (body too large, admin denied)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _envelope(
    code: str,
    message: str,
    request_id: str,
    *,
    attempts: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "message": message,
        "type": "invalid_request_error" if code in _CLIENT_CODES else "server_error",
        "code": code,
        "param": None,
        "request_id": request_id,
    }
    if attempts:
        err["attempts"] = attempts  # provider / model / outcome / ms — never prompt content
    err.update(extra)
    return {"error": err}


_CLIENT_CODES = frozenset(
    {
        "invalid_request",
        "capability_unavailable",
        "context_overflow",
        "model_not_found",
        "key_invalid",
        "guardrail_blocked",
        "rate_limited",
    }
)


@dataclass
class _AuthResult:
    key_hash: str | None
    allow: list[str]
    tenant: str
    rpm_limit: int | None
    tpm_limit: int | None


@dataclass
class _CacheCompletion:
    """Duck-types the ``.text``/``.completion_tokens``/``.cost_usd`` a B3 cache store hook expects.

    :func:`examlops.semantic_cache.bind_to_gateway` was written against
    :class:`examlops.gateway.Completion`; this service has its own :class:`ChatResult` shape, so a
    tiny shim is cheaper and clearer than importing the client's dataclass just for these 3 fields.
    """

    text: str
    completion_tokens: int = 0
    cost_usd: float = 0.0


def prepare_messages(
    messages: list[dict[str, Any]],
    tenant: str,
    guard: Any,
    prompt_ref: str | None,
) -> list[dict[str, Any]]:
    """Guard-scan the caller's own messages, then prepend a registry prompt template if named.

    Same order and reasoning :class:`examlops.gateway.GatewayClient` uses: only the caller's text
    is untrusted input, so only it is scanned before dispatch — a registry template is reviewed,
    versioned text and must not block every request it serves. Runs in a worker thread (guard
    scanning and prompt lookup are both synchronous, occasionally-blocking calls).
    """
    if guard is not None:
        messages = _guard_messages(guard, messages, tenant)
    if prompt_ref:
        try:
            template, _name, _version = resolve_prompt_ref(prompt_ref)
        except LookupError as exc:
            raise ProviderError("invalid_request", str(exc)) from exc
        messages = [{"role": "system", "content": template}, *messages]
    return messages


class _State:
    """Everything mutable in the app. One reference to the routing table, swapped atomically."""

    def __init__(self, loader: Callable[[], Awaitable[Runtime]]) -> None:
        self.loader = loader
        self.runtime: Runtime | None = None
        self.lock = asyncio.Lock()
        self.latched = False
        self.last_reload_error: list[str] | None = None
        self.probes: dict[str, ProbeResult] = {}
        self.probed_at = 0.0
        self.background: set[asyncio.Task[Any]] = set()
        self.guardrails: dict[str, Any] = {}  # tenant -> guard instance or None, built once

    async def ensure_runtime(self) -> Runtime:
        if self.runtime is None:
            async with self.lock:
                if self.runtime is None:
                    self.runtime = await self.loader()
        return self.runtime

    async def probe_all(self, rt: Runtime, max_age: float = 5.0) -> dict[str, ProbeResult]:
        if time.monotonic() - self.probed_at > max_age or set(self.probes) != set(rt.providers):
            names = list(rt.providers)
            results = await asyncio.gather(*(rt.providers[n].probe() for n in names))
            self.probes = dict(zip(names, results, strict=True))
            self.probed_at = time.monotonic()
        return self.probes

    def spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def guardrail_for(self, tenant: str) -> Any:
        """The D8 guardrail for one tenant (ADR 0026 clause 3), built once and cached.

        ``default_guardrail`` may construct an optional NER model; building it per-request would
        pay that cost on every call, so each tenant's instance — or its absence, when
        ``EXAMLOPS_GUARDRAIL_MODE=off`` — is resolved once and reused for the process lifetime.
        """
        if tenant not in self.guardrails:
            self.guardrails[tenant] = default_guardrail(tenant)
        return self.guardrails[tenant]


class _Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        reg = self.registry
        self.requests = Counter(
            "llm_gateway_requests",
            "Requests by route, serving provider and model, HTTP status and error code.",
            ["route", "provider", "model", "status", "code"],
            registry=reg,
        )
        self.seconds = Histogram(
            "llm_gateway_request_seconds", "End-to-end request time.", ["route"], registry=reg
        )
        self.ttft = Histogram(
            "llm_gateway_ttft_seconds",
            "Time to first token (includes any model load).",
            ["route", "provider"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
            registry=reg,
        )
        self.tpot = Histogram(
            "llm_gateway_tpot_seconds",
            "Time per output token after the first (ADR 0156 d2, ADR 0148 TTFT/TPOT pair) — "
            "(total request time - TTFT) / (completion tokens - 1). Not per-token: providers "
            "report a first-token timestamp and a final token count, not one timestamp per token.",
            ["route", "provider"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
            registry=reg,
        )
        self.tokens = Counter("llm_gateway_tokens", "Tokens by kind.", ["kind"], registry=reg)
        self.cache = Counter(
            "llm_gateway_cache", "B3 semantic-cache lookups by result.", ["result"], registry=reg
        )
        self.denials = Counter(
            "llm_gateway_policy_denials", "Requests refused by policy.", ["reason"], registry=reg
        )
        self.reloads = Counter(
            "llm_gateway_config_reload", "Config reloads by result.", ["result"], registry=reg
        )
        self.breaker = Gauge(
            "llm_gateway_breaker_state",
            "Circuit breaker: 0 closed, 1 half-open, 2 open.",
            ["provider", "model"],
            registry=reg,
        )
        self.inflight = Gauge(
            "llm_gateway_inflight", "Requests in flight.", ["provider", "model"], registry=reg
        )
        self.queue_depth = Gauge(
            "llm_gateway_queue_depth",
            "Requests waiting for a bulkhead slot (ADR 0153 d6).",
            ["provider", "model"],
            registry=reg,
        )
        self.provider_up = Gauge(
            "llm_gateway_provider_up",
            "1 when the provider's last probe succeeded.",
            ["provider"],
            registry=reg,
        )
        self.retries = Counter(
            "llm_gateway_retries",
            "Attempts beyond the first, by the prior attempt's outcome (ADR 0156 d2).",
            ["reason"],
            registry=reg,
        )
        self.fallbacks = Counter(
            "llm_gateway_fallbacks",
            "Switches from one deployment to another within a single request.",
            ["from_provider", "to_provider"],
            registry=reg,
        )


def _check_admin_token(token: str) -> str:
    if token and (token.lower() in _WEAK_TOKENS or len(token) < 16):
        raise ValueError(
            "the LLM gateway admin token is a placeholder or shorter than 16 characters; "
            "set LLM_GATEWAY_ADMIN_TOKEN to a real secret (or leave it empty to disable the admin API)"
        )
    return token


def _request_id(request: Request) -> str:
    given = request.headers.get("x-request-id", "")
    return given if _RID.fullmatch(given) else f"req_{uuid.uuid4().hex[:24]}"


def _sse(obj: Any) -> str:
    return f"data: {obj if isinstance(obj, str) else json.dumps(obj, separators=(',', ':'))}\n\n"


def _tpot_ms(total_ms: float, ttft_ms: float | None, completion_tokens: int) -> float | None:
    """Time per output token after the first, or ``None`` when it cannot be computed.

    Needs a real TTFT and at least 2 completion tokens (1 token has no "after the first" to time).
    A degenerate negative result (a provider's own timing was inconsistent) is also refused rather
    than recorded, since a negative duration would silently corrupt the histogram's buckets.
    """
    if ttft_ms is None or completion_tokens <= 1:
        return None
    tpot = (total_ms - ttft_ms) / (completion_tokens - 1)
    return tpot if tpot > 0 else None


def create_app(
    *,
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    provider_factory: ProviderFactory = default_provider_factory,
    auth: str | None = None,
    admin_token: str | None = None,
    max_body_bytes: int = _MAX_BODY,
    probe_ttl_s: float = 5.0,
) -> FastAPI:
    auth_mode = (auth or os.getenv("LLM_GATEWAY_AUTH") or "keys").lower()
    if auth_mode not in ("keys", "off"):
        raise ValueError(f"LLM_GATEWAY_AUTH must be 'keys' or 'off', got {auth_mode!r}")
    if auth_mode == "off":
        logger.warning(
            "LLM gateway auth is OFF: any caller that can reach the port can use every model"
        )
    token = _check_admin_token(
        os.getenv("LLM_GATEWAY_ADMIN_TOKEN", "") if admin_token is None else admin_token
    )
    metrics = _Metrics()
    # D8/B3 wiring (BL-103, 2026-09-23): guardrails always run (governed by the same
    # EXAMLOPS_GUARDRAIL_MODE the in-process GatewayClient honours, default "monitor" — scan and
    # record, never block, until an operator opts into "enforce"). The semantic cache changes
    # response behaviour for repeat-ish prompts, so it stays opt-in per deployment.
    cache_enabled = _truthy(os.getenv("LLM_GATEWAY_SEMANTIC_CACHE", ""))
    semantic_cache = SemanticCache() if cache_enabled else None

    async def load() -> Runtime:
        if config is not None:
            return await build_runtime(config, provider_factory=provider_factory, source="file")
        path = Path(config_path) if config_path else default_config_path()
        if path is not None:
            return await build_runtime(
                load_config_file(path), provider_factory=provider_factory, source="file"
            )
        url = os.getenv("EXAMLOPS_LLM_OLLAMA_URL", "http://127.0.0.1:11434")
        return await build_runtime(
            generated_config(url), provider_factory=provider_factory, source="generated"
        )

    state = _State(load)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        rt = await state.ensure_runtime()  # an invalid config fails the start, loudly
        logger.info(
            "llm-gateway up: %d routes, source=%s, warnings=%d, cache=%s",
            len(rt.catalog.routes),
            rt.source,
            len(rt.warnings),
            "on" if semantic_cache is not None else "off",
        )
        yield

    app = FastAPI(
        title="ExaMLOps LLM Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = state

    # ── errors ───────────────────────────────────────────────────────────────

    def error_response(
        exc: Exception,
        rid: str,
        route: str = "unknown",
        provider: str = "none",
        model: str = "unknown",
    ) -> JSONResponse:
        headers = {"x-request-id": rid}
        if isinstance(exc, ProviderError):
            code, status, message, attempts = exc.kind, exc.http_status, exc.message, exc.attempts
            if exc.retry_after:
                headers["retry-after"] = str(max(1, round(exc.retry_after)))
        elif isinstance(exc, KeyInvalid):
            code, status, message, attempts = "key_invalid", 401, str(exc), []
            metrics.denials.labels("key").inc()
        elif isinstance(exc, ModelNotAllowed):
            code, status, message, attempts = "model_not_allowed", 403, str(exc), []
            metrics.denials.labels("model").inc()
        elif isinstance(exc, BudgetExceeded):
            code, status, message, attempts = "budget_exceeded", 429, str(exc), []
            metrics.denials.labels("budget").inc()
        elif isinstance(exc, GuardrailBlocked):
            code, status, message, attempts = "guardrail_blocked", 400, str(exc), []
            metrics.denials.labels("guardrail").inc()
        elif isinstance(exc, _Http):
            code, status, message, attempts = exc.code, exc.status, exc.message, []
        else:
            logger.exception("unhandled error in request %s", rid)
            code, status, message, attempts = "internal_error", 500, "internal gateway error", []
        if code == "locality_denied":
            metrics.denials.labels("locality").inc()
        elif code == "rate_limited":
            metrics.denials.labels("rate_limit").inc()
            # A fixed 60s window: an honest upper bound (the window can reset sooner), not a
            # promise — coord_rate_allow's boolean result carries no exact remaining-time.
            headers.setdefault("retry-after", "60")
        metrics.requests.labels(route, provider, model, str(status), code).inc()
        return JSONResponse(
            _envelope(code, message, rid, attempts=attempts), status, headers=headers
        )

    # ── auth ─────────────────────────────────────────────────────────────────

    def bearer(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        return header[7:].strip() if header[:7].lower() == "bearer " else None

    async def authenticate(request: Request, model: str | None) -> _AuthResult:
        """Off ⇒ an unlimited, keyless result (tenant "default")."""
        if auth_mode == "off":
            return _AuthResult(None, [], "default", None, None)
        raw = bearer(request)
        if not raw:
            raise KeyInvalid("missing bearer token: send `Authorization: Bearer <virtual key>`")
        rec: dict[str, Any] | None
        if model is not None:
            rec = await asyncio.to_thread(authorize, raw, model)
        else:  # listing models: the key must be valid; the allow-list narrows what it sees
            from examlops.data.gateway import get_virtual_key

            rec = await asyncio.to_thread(get_virtual_key, _hash_key(raw))
            if rec is None or rec.get("revoked"):
                raise KeyInvalid("unknown or revoked virtual key")
        if rec is None:  # unreachable: authorize() raises instead of returning None
            raise KeyInvalid("unknown or revoked virtual key")
        return _AuthResult(
            key_hash=_hash_key(raw),
            allow=list(rec.get("models") or []),
            tenant=str(rec.get("tenant") or "default"),
            rpm_limit=rec.get("rpm_limit"),
            tpm_limit=rec.get("tpm_limit"),
        )

    async def enforce_rate_limits(auth: _AuthResult) -> None:
        """RPM/TPM caps (BL-107), checked before any dispatch. No-op for an unkeyed caller —
        auth mode "off" has no virtual key to attach a limit to, same as budget/allow-list."""
        if auth.key_hash is None or (auth.rpm_limit is None and auth.tpm_limit is None):
            return
        from examlops.coordination import get_coordinator

        coordinator = get_coordinator()
        if auth.rpm_limit is not None:
            allowed = await asyncio.to_thread(
                coordinator.allow, f"gateway:rpm:{auth.key_hash}", auth.rpm_limit, 60.0
            )
            if not allowed:
                raise _Http(
                    429, "rate_limited", f"requests-per-minute limit ({auth.rpm_limit}) exceeded"
                )
        if auth.tpm_limit is not None:
            # amount=0: a pure read-only probe — the current window's actual cost isn't known
            # until the response completes (record_token_usage records it then).
            allowed = await asyncio.to_thread(
                coordinator.allow, f"gateway:tpm:{auth.key_hash}", auth.tpm_limit, 60.0, amount=0
            )
            if not allowed:
                raise _Http(
                    429, "rate_limited", f"tokens-per-minute limit ({auth.tpm_limit}) exceeded"
                )

    async def record_token_usage(auth: _AuthResult, total_tokens: int) -> None:
        """Charge a completed call's real token count against the TPM window (BL-107)."""
        if auth.key_hash is None or auth.tpm_limit is None or total_tokens <= 0:
            return
        from examlops.coordination import get_coordinator

        coordinator = get_coordinator()
        await asyncio.to_thread(
            coordinator.allow,
            f"gateway:tpm:{auth.key_hash}",
            auth.tpm_limit,
            60.0,
            amount=total_tokens,
        )

    def require_admin(request: Request) -> None:
        if not token:
            raise _Http(
                503, "gateway_unavailable", "admin API disabled: LLM_GATEWAY_ADMIN_TOKEN is not set"
            )
        given = bearer(request) or ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise _Http(401, "key_invalid", "admin token required")

    # ── accounting ───────────────────────────────────────────────────────────

    def record(
        rt: Runtime,
        route: str,
        key_hash: str | None,
        provider: str,
        usage: Any,
        ms: float,
        *,
        error: bool,
    ) -> None:
        from examlops.data.finops import add_key_spend
        from examlops.data.gateway import record_gateway_call

        locality = rt.providers[provider].locality if provider in rt.providers else "external"
        prompt = getattr(usage, "prompt_tokens", 0)
        completion = getattr(usage, "completion_tokens", 0)
        # A local model has no per-token price; only an external one is billed (ADR 0156 d6).
        cost = 0.0 if locality in ("local", "site") else _estimate_cost(route, prompt, completion)
        _account(
            "usage" if not error else "error",
            record_gateway_call,
            key_hash,
            route,
            backend=provider if provider != "none" else None,
            cost_usd=cost,
            prompt_tokens=prompt,
            completion_tokens=completion,
            latency_ms=ms,
            error=error,
        )
        if key_hash and cost:
            _account("key spend", add_key_spend, key_hash, cost)

    def record_retries_and_fallbacks(attempts: list[dict[str, Any]]) -> None:
        """Every attempt after the first in one request is both a retry (of the request) and a
        fallback (to the next deployment `GatewayCore`'s candidate loop moved to) — the loop never
        retries the same deployment twice, so the two events always coincide here."""
        for prev, cur in zip(attempts, attempts[1:], strict=False):
            metrics.retries.labels(prev["outcome"]).inc()
            metrics.fallbacks.labels(prev["provider"], cur["provider"]).inc()

    # ── /v1/chat/completions ─────────────────────────────────────────────────

    async def read_body(request: Request) -> _ChatBody:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_body_bytes:
            raise _Http(413, "invalid_request", f"request body exceeds {max_body_bytes} bytes")
        raw = await request.body()
        if len(raw) > max_body_bytes:
            raise _Http(413, "invalid_request", f"request body exceeds {max_body_bytes} bytes")
        try:
            return _ChatBody.model_validate_json(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "body"
            raise ProviderError("invalid_request", f"{where}: {first['msg']}") from None

    def build_request(
        body: _ChatBody, rt: Runtime, messages: list[dict[str, Any]]
    ) -> tuple[ChatRequest, tuple[str, ...]]:
        hints = body.examlops or (body.extra_body or {}).get("examlops") or {}
        allowed = rt.allowed_localities
        if isinstance(hints.get("allowed_localities"), list):  # a caller may narrow, never widen
            allowed = tuple(loc for loc in allowed if loc in hints["allowed_localities"])
        stop = [body.stop] if isinstance(body.stop, str) else body.stop
        req = ChatRequest(
            model=body.model,
            messages=messages,
            temperature=body.temperature,
            top_p=body.top_p,
            max_tokens=body.max_completion_tokens or body.max_tokens,
            stop=stop,
            seed=body.seed,
            tools=body.tools,
            tool_choice=body.tool_choice,
            response_format=body.response_format,
            extra=dict(hints.get("ollama") or {}),
        )
        return req, allowed

    def completion_json(result: ChatResult, model: str, rid: str) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": result.text}
        if result.tool_calls:
            message["tool_calls"] = result.tool_calls
            message["content"] = result.text or None
        if result.reasoning:
            message["reasoning_content"] = result.reasoning
        return {
            "id": f"chatcmpl-{rid}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "total_tokens": result.usage.prompt_tokens + result.usage.completion_tokens,
            },
        }

    def chunk_json(chunk: ChatChunk, model: str, cid: str, first: bool) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        if first:
            delta["role"] = "assistant"
        if chunk.text:
            delta["content"] = chunk.text
        if chunk.reasoning:
            delta["reasoning_content"] = chunk.reasoning
        if chunk.tool_calls:
            delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(chunk.tool_calls)]
        return {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": chunk.finish_reason}],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        """The one HTTP entry point every caller (Skipper, RAG, `exa gateway`, a router) uses.

        Order on the non-streaming path: authenticate → D8 **input** scan + B1 prompt-ref
        (``prepare_messages``) → B3 cache lookup → dispatch (skipped on a cache hit) → accounting
        (skipped on a cache hit — no cost was incurred) → D8 **output** scan → B3 cache store
        (skipped on a cache hit — it is already in the cache). This mirrors
        :meth:`examlops.gateway.GatewayClient.chat` exactly, clause for clause.

        The streaming path gets the same input scan and prompt-ref resolution — the request is no
        less untrusted for asking to stream — but not the output scan or the cache: blocking or
        redacting a response after tokens have already been sent to the caller does not undo the
        send, and a cache keyed on a complete answer has nothing to key a partial one on. Closing
        that gap needs a buffering or chunk-level design this pass does not attempt (queued,
        `.claude/plans/BACKLOG.md` BL-115).
        """
        rid = _request_id(request)
        started = time.perf_counter()
        route_label, provider, upstream = "unknown", "none", "unknown"
        rt: Runtime | None = None
        key_hash: str | None = None
        try:
            rt = await state.ensure_runtime()
            body = await read_body(request)
            route = rt.catalog.resolve(body.model)
            route_label = route.name if route else "unknown"
            auth = await authenticate(request, body.model)
            key_hash, tenant = auth.key_hash, auth.tenant
            await enforce_rate_limits(auth)
            hints = body.examlops or (body.extra_body or {}).get("examlops") or {}
            guard = state.guardrail_for(tenant)
            messages = await asyncio.to_thread(
                prepare_messages, body.messages, tenant, guard, hints.get("prompt_ref")
            )
            req, allowed = build_request(body, rt, messages)
            budget = request.headers.get("x-examlops-budget-ms")
            budget_ms = float(budget) if budget and budget.replace(".", "", 1).isdigit() else None
            attempts: list[dict[str, Any]] = []
            attempts_recorded = False  # guards against double-counting on a later exception

            cache_kw = {
                "temperature": body.temperature,
                "max_tokens": req.max_tokens,
                "top_p": body.top_p,
                "stop": req.stop,
                "seed": body.seed,
            }
            cache_params = _cache_params(cache_kw, body.response_format)
            use_cache = (
                semantic_cache is not None
                and not body.stream
                and _cacheable(messages)
                and not bool(hints.get("no_cache"))
            )

            if not body.stream:
                result: ChatResult | None = None
                cache_hit = False
                if use_cache:
                    assert semantic_cache is not None  # implied by use_cache's own condition
                    lookup, _store = bind_to_gateway(semantic_cache, tenant)
                    hit_text = await asyncio.to_thread(lookup, body.model, messages, cache_params)
                    if hit_text is not None:
                        result = ChatResult(
                            text=hit_text, model=body.model, provider="cache", usage=Usage()
                        )
                        cache_hit = True

                if result is None:
                    result = await rt.core.chat(
                        body.model,
                        req,
                        allowed_localities=allowed,
                        caller_budget_ms=budget_ms,
                        attempts=attempts,
                    )

                provider, upstream = result.provider, result.model
                ms = (time.perf_counter() - started) * 1000.0
                if not cache_hit:  # a cache hit spent nothing and reused an already-accounted call
                    await asyncio.to_thread(
                        record, rt, route_label, key_hash, provider, result.usage, ms, error=False
                    )
                    await record_token_usage(
                        auth, result.usage.prompt_tokens + result.usage.completion_tokens
                    )
                record_retries_and_fallbacks(attempts)
                attempts_recorded = True
                metrics.requests.labels(route_label, provider, upstream, "200", "ok").inc()
                metrics.seconds.labels(route_label).observe(ms / 1000.0)
                if result.ttft_ms is not None:
                    metrics.ttft.labels(route_label, provider).observe(result.ttft_ms / 1000.0)
                tpot = _tpot_ms(ms, result.ttft_ms, result.usage.completion_tokens)
                if tpot is not None:
                    metrics.tpot.labels(route_label, provider).observe(tpot / 1000.0)
                metrics.tokens.labels("prompt").inc(result.usage.prompt_tokens)
                metrics.tokens.labels("completion").inc(result.usage.completion_tokens)

                # D8 outbound scan (ADR 0026 clause 3). After accounting on purpose: the tokens, if
                # any were spent, are already billed, so a blocked answer disappearing must not
                # make the bill disagree with the provider's. Before the cache store, so a blocked
                # answer is never cached and a redacted one is cached redacted.
                if guard is not None:
                    verdict = await asyncio.to_thread(
                        guard.check_output, result.text, {"tenant": tenant, "model": body.model}
                    )
                    if verdict.blocked:
                        raise GuardrailBlocked("response", verdict.findings, verdict.reason)
                    result.text = verdict.text

                if use_cache and not cache_hit:
                    assert semantic_cache is not None  # implied by use_cache's own condition
                    _lookup, store = bind_to_gateway(semantic_cache, tenant)
                    await asyncio.to_thread(
                        store,
                        body.model,
                        messages,
                        _CacheCompletion(result.text, result.usage.completion_tokens),
                        cache_params,
                    )

                headers = {
                    "x-request-id": rid,
                    "x-examlops-route": route_label,
                    "x-examlops-provider": provider,
                    "x-examlops-model": upstream,
                }
                if use_cache:
                    result_label = "hit" if cache_hit else "miss"
                    headers["x-examlops-cache"] = result_label
                    metrics.cache.labels(result_label).inc()
                if result.load_ms and result.load_ms >= 500:
                    headers["x-examlops-cold"] = (
                        "1"  # the model was loaded for this request (ADR 0153 d8)
                    )
                return JSONResponse(completion_json(result, body.model, rid), headers=headers)

            gen = rt.core.chat_stream(
                body.model,
                req,
                allowed_localities=allowed,
                caller_budget_ms=budget_ms,
                attempts=attempts,
            )
            first = await anext(
                gen, None
            )  # a failure before the first token is still a proper HTTP error
            served = attempts[-1] if attempts else {}
            provider, upstream = served.get("provider", "none"), served.get("model", "unknown")
            record_retries_and_fallbacks(attempts)  # pre-first-token failovers only (ADR 0153 d9)
            attempts_recorded = True
        except Exception as exc:  # noqa: BLE001 - every failure leaves as the typed envelope
            if "attempts" in locals() and not attempts_recorded:
                record_retries_and_fallbacks(attempts)
            if (
                rt is not None
                and route_label != "unknown"
                and not isinstance(
                    exc,
                    _Http | KeyInvalid | ModelNotAllowed | BudgetExceeded | GuardrailBlocked,
                )
            ):
                ms = (time.perf_counter() - started) * 1000.0
                await asyncio.to_thread(
                    record, rt, route_label, key_hash, "none", None, ms, error=True
                )
            return error_response(exc, rid, route_label, provider, upstream)

        include_usage = bool((body.stream_options or {}).get("include_usage"))
        cid = f"chatcmpl-{rid}"

        async def events() -> AsyncIterator[str]:
            usage = None
            sent_role = False
            failed = False
            ttft_seen: float | None = None
            try:
                stream: AsyncIterator[ChatChunk] = _prepend(first, gen)
                async for chunk in stream:
                    if chunk.ttft_ms is not None:
                        ttft_seen = chunk.ttft_ms
                        metrics.ttft.labels(route_label, provider).observe(chunk.ttft_ms / 1000.0)
                    if chunk.usage is not None:
                        usage = chunk.usage
                    yield _sse(chunk_json(chunk, body.model, cid, not sent_role))
                    sent_role = True
                if include_usage and usage is not None:
                    yield _sse(
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": body.model,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "total_tokens": usage.prompt_tokens + usage.completion_tokens,
                            },
                        }
                    )
            except ProviderError as exc:
                failed = True
                env = _envelope(exc.kind, exc.message, rid, attempts=exc.attempts, partial=True)
                yield _sse(env)
            finally:
                await gen.aclose()
                ms = (time.perf_counter() - started) * 1000.0
                status, code = ("502", "stream_interrupted") if failed else ("200", "ok")
                metrics.requests.labels(route_label, provider, upstream, status, code).inc()
                metrics.seconds.labels(route_label).observe(ms / 1000.0)
                if usage is not None:
                    metrics.tokens.labels("prompt").inc(usage.prompt_tokens)
                    metrics.tokens.labels("completion").inc(usage.completion_tokens)
                    tpot = _tpot_ms(ms, ttft_seen, usage.completion_tokens)
                    if tpot is not None:
                        metrics.tpot.labels(route_label, provider).observe(tpot / 1000.0)
                assert rt is not None
                state.spawn(
                    asyncio.to_thread(
                        record, rt, route_label, key_hash, provider, usage, ms, error=failed
                    )
                )
                if usage is not None and not failed:
                    state.spawn(
                        record_token_usage(auth, usage.prompt_tokens + usage.completion_tokens)
                    )
            yield _sse("[DONE]")

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "x-request-id": rid,
                "x-examlops-route": route_label,
                "x-examlops-provider": provider,
                "x-examlops-model": upstream,
                "cache-control": "no-cache",
            },
        )

    # ── /v1/models, health, readiness ────────────────────────────────────────

    @app.get("/v1/models")
    async def list_models(request: Request) -> Response:
        rid = _request_id(request)
        try:
            rt = await state.ensure_runtime()
            allow = (await authenticate(request, None)).allow
            snap = rt.core.snapshot()
            data = []
            for name, route in rt.catalog.routes.items():
                if all(snap[d.key]["breaker"] == "open" for d in route.deployments):
                    continue  # listed models are ones that can currently be served
                data.append((name, route.deployments[0].provider.name))
            aliases = [
                (a, rt.catalog.routes[t].deployments[0].provider.name)
                for a, t in rt.catalog.aliases.items()
                if t in rt.catalog.routes
            ]
            listing = [
                {"id": n, "object": "model", "created": int(rt.built_at), "owned_by": owner}
                for n, owner in data + aliases
                if not allow or n in allow
            ]
            return JSONResponse({"object": "list", "data": listing}, headers={"x-request-id": rid})
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, rid)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        rt = await state.ensure_runtime()
        probes = await state.probe_all(rt, max_age=probe_ttl_s)
        snap = rt.core.snapshot()
        routes: dict[str, dict[str, Any]] = {}
        for name, route in rt.catalog.routes.items():
            healthy = any(
                probes[d.provider.name].ok and snap[d.key]["breaker"] != "open"
                for d in route.deployments
            )
            routes[name] = {
                "healthy": healthy,
                "required": route.required,
                "deployments": len(route.deployments),
            }
        required = [r for r in routes.values() if r["required"]] or []
        healthy_now = bool(routes) and (
            all(r["healthy"] for r in required)
            if required
            else any(r["healthy"] for r in routes.values())
        )
        state.latched = (
            state.latched or healthy_now
        )  # never un-ready on a later outage (ADR 0153 d10)
        return JSONResponse(
            {
                "ready": state.latched,
                "healthy_now": healthy_now,
                "latched": state.latched and not healthy_now,
                "routes": routes,
                "warnings": rt.warnings,
            },
            status_code=200 if state.latched else 503,
        )

    # ── admin ────────────────────────────────────────────────────────────────

    def route_summary(rt: Runtime) -> dict[str, Any]:
        return {
            name: {
                "strategy": r.strategy,
                "required": r.required,
                "fallbacks": r.fallbacks,
                "deployments": [
                    {"provider": d.provider.name, "model": d.model, "priority": d.priority}
                    for d in r.deployments
                ],
            }
            for name, r in rt.catalog.routes.items()
        }

    @app.get("/admin/health")
    async def admin_health(request: Request) -> Response:
        rid = _request_id(request)
        try:
            require_admin(request)
            rt = await state.ensure_runtime()
            probes = await state.probe_all(rt, max_age=0.0)
            providers = {
                n: {
                    "ok": pr.ok,
                    "latency_ms": round(pr.latency_ms, 1),
                    "detail": pr.detail,
                    "resident": pr.resident,
                    "type": rt.providers[n].type,
                    "locality": rt.providers[n].locality,
                }
                for n, pr in probes.items()
            }
            return JSONResponse({"providers": providers, "deployments": rt.core.snapshot()})
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, rid)

    @app.get("/admin/config")
    async def admin_config(request: Request) -> Response:
        rid = _request_id(request)
        try:
            require_admin(request)
            rt = await state.ensure_runtime()
            return JSONResponse(
                {
                    "source": rt.source,
                    "built_at": rt.built_at,
                    "auth": auth_mode,
                    "warnings": rt.warnings,
                    "last_reload_error": state.last_reload_error,
                    "aliases": rt.catalog.aliases,
                    "routes": route_summary(rt),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, rid)

    @app.post("/admin/reload")
    async def admin_reload(request: Request) -> Response:
        rid = _request_id(request)
        try:
            require_admin(request)
            await state.ensure_runtime()
            try:
                new = await state.loader()
            except ConfigError as exc:  # keep serving the last good table; say why
                state.last_reload_error = exc.errors
                metrics.reloads.labels("rejected").inc()
                body = _envelope(
                    "config_invalid", "config rejected; the previous one is still serving", rid
                )
                body["error"]["errors"] = exc.errors
                return JSONResponse(body, status_code=422, headers={"x-request-id": rid})
            state.runtime, state.last_reload_error = new, None
            metrics.reloads.labels("applied").inc()
            return JSONResponse(
                {"reloaded": True, "routes": sorted(new.catalog.routes), "warnings": new.warnings}
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, rid)

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        rid = _request_id(request)
        try:
            require_admin(request)
            rt = await state.ensure_runtime()
            probes = await state.probe_all(
                rt, max_age=max(probe_ttl_s, 10.0 if probe_ttl_s else 0.0)
            )
            for name, pr in probes.items():
                metrics.provider_up.labels(name).set(1 if pr.ok else 0)
            for route in rt.catalog.routes.values():
                snap = rt.core.snapshot()
                for d in route.deployments:
                    s = snap[d.key]
                    metrics.breaker.labels(d.provider.name, d.model).set(
                        _BREAKER_VALUE[s["breaker"]]
                    )
                    metrics.inflight.labels(d.provider.name, d.model).set(s["inflight"])
                    metrics.queue_depth.labels(d.provider.name, d.model).set(s["queue_depth"])
            return Response(
                generate_latest(metrics.registry), media_type="text/plain; version=0.0.4"
            )
        except Exception as exc:  # noqa: BLE001
            return error_response(exc, rid)

    return app


async def _prepend(
    first: ChatChunk | None, rest: AsyncIterator[ChatChunk]
) -> AsyncIterator[ChatChunk]:
    if first is not None:
        yield first
    async for chunk in rest:
        yield chunk


__all__ = ["GatewayError", "create_app"]

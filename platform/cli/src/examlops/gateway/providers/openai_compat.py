"""OpenAI-compatible provider — the generic adapter for routers (ADR 0152 d3).

The one adapter every OpenAI-shaped upstream shares: OmniRoute, LiteLLM, OpenRouter, another
ExaMLOps gateway, or a self-hosted engine's own ``/v1`` (vLLM/SGLang). Unlike
:class:`~examlops.gateway.providers.ollama.OllamaProvider`, :class:`ChatRequest` is already in
this wire shape (messages, tools, ``response_format``) — this adapter mostly passes requests
through rather than translating them, and classifies whatever comes back.

**Everything this adapter reports is the upstream's own claim, never re-derived** (design spec
§14: "router upstream semantics differ from OpenAI" is a named, accepted risk, not assumed away).
A router answering an ``auto``/aliased model name may report a *different* concrete model in its
response — that's passed straight through as :attr:`ChatResult.model`, the same way the request's
own ``model`` field is; ExaMLOps records it, it does not verify it.

Most real-world OpenAI-compat servers agree closely enough that no per-vendor subclass is needed;
the handful of places they diverge (whether an unrecognised ``stream_options`` field 400s, whether
a rate-limit retry hint is seconds or milliseconds, whether errors are wrapped in ``{"error": …}``
or something else) are one small :class:`OpenAICompatQuirks` knob each, not a fork. Per the plan
(P5), this is unverified against a live router until an owner-deployed instance exists — the
defaults below match the documented OpenAI Chat Completions API and vLLM/SGLang's own ``/v1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from examlops.gateway.egress import (
    check_resolved_addresses,
    guarded_async_client,
    validate_base_url,
)
from examlops.gateway.providers.base import (
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChatResult,
    EmbedResult,
    ModelInfo,
    ProbeResult,
    ProviderError,
    Usage,
)

#: Bound on the per-connection DNS-rebinding re-check (BL-111) — same budget as the Ollama adapter.
_RESOLVE_TIMEOUT_S = 2.0


@dataclass
class OpenAICompatQuirks:
    """The real-world deviations from the documented API this adapter has actually needed to
    tolerate (PLAN.md P5: "vendor-quirk table"). Defaults match a spec-compliant server; flip a
    knob for a router that does not."""

    #: Request ``stream_options: {"include_usage": true}`` on a streamed call. A server that 400s
    #: on an unrecognised field (rather than ignoring it, as the spec requires) needs this off —
    #: usage on that stream then comes back ``estimated`` rather than measured.
    send_stream_options: bool = True
    #: A 429's retry hint, when present only in the error body (no ``Retry-After`` header), is
    #: read from ``error.retry_after``. Some routers report that in milliseconds, not seconds.
    retry_after_is_ms: bool = False


def _finish(tool_calls: list[dict[str, Any]]) -> str:
    return "tool_calls" if tool_calls else "stop"


def _error_message(data: Any) -> str:
    """OpenAI wraps errors in ``{"error": {"message": …}}``; not every router agrees — fall
    through the shapes actually seen in the wild rather than assume one."""
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    if isinstance(err, str):
        return err
    for key in ("message", "detail"):  # FastAPI-style routers (OmniRoute is FastAPI-based)
        if key in data:
            return str(data[key])
    return ""


class OpenAICompatProvider:
    """One OpenAI-shaped upstream: a router, another gateway, or an engine's own ``/v1``."""

    type = "openai_compat"

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        api_key: str | None = None,
        locality: str = "external",
        constrains_schema: bool = False,
        quirks: OpenAICompatQuirks | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.locality = locality
        self._allowed_hosts = allowed_hosts
        self.base_url = validate_base_url(base_url, locality=locality, allowed_hosts=allowed_hosts)
        self._api_key = api_key
        #: Whether this specific upstream is trusted to honour ``response_format.json_schema``
        #: strictly (real OpenAI and vLLM's own ``/v1`` do; an unknown router is not assumed to) —
        #: operator-set via config, never inferred, because a false positive here means the
        #: gateway skips its own output validation on an upstream that never enforced anything.
        self.constrains_schema = constrains_schema
        self.quirks = quirks or OpenAICompatQuirks()
        # Same rationale as the Ollama adapter: fail fast on connect, generous on read (a router
        # fanning out to a cold backend can take as long as the backend itself does).
        self.timeout = timeout or httpx.Timeout(300.0, connect=5.0)
        self._transport = transport
        self._shared = client

    # ── plumbing (identical shape to OllamaProvider — see its docstrings for the "why") ────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _precheck_resolution(self) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    check_resolved_addresses,
                    self.base_url,
                    locality=self.locality,
                    allowed_hosts=self._allowed_hosts,
                ),
                timeout=_RESOLVE_TIMEOUT_S,
            )
        except TimeoutError:
            pass

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._shared is not None:
            await self._precheck_resolution()
            yield self._shared
            return
        if self._transport is not None:
            await self._precheck_resolution()
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                yield client
            return
        async with guarded_async_client(
            self.base_url,
            locality=self.locality,
            allowed_hosts=self._allowed_hosts,
            timeout=self.timeout,
        ) as client:
            yield client

    def _err(self, kind: str, message: str, **kw: Any) -> ProviderError:
        return ProviderError(kind, message, provider=self.name, **kw)

    def _from_exception(self, exc: Exception, *, committed: bool = False) -> ProviderError:
        if committed:
            return self._err(
                "stream_interrupted", f"upstream dropped mid-stream: {exc}", retryable=False
            )
        if isinstance(exc, httpx.ConnectTimeout | httpx.ConnectError):
            return self._err("upstream_unavailable", f"cannot connect to {self.base_url}: {exc}")
        if isinstance(exc, httpx.TimeoutException):
            return self._err("upstream_timeout", f"{self.base_url} timed out: {exc}")
        if isinstance(exc, httpx.TransportError):
            return self._err("upstream_error", f"transport error talking to {self.base_url}: {exc}")
        return self._err("upstream_error", str(exc))

    def _retry_after(self, resp: httpx.Response, data: Any) -> float | None:
        header = resp.headers.get("Retry-After", "")
        if header:
            with contextlib.suppress(ValueError):
                return float(header)
        if isinstance(data, dict):
            err = data.get("error")
            raw = err.get("retry_after") if isinstance(err, dict) else None
            if raw is not None:
                with contextlib.suppress(ValueError, TypeError):
                    return float(raw) / 1000.0 if self.quirks.retry_after_is_ms else float(raw)
        return None

    def _from_response(self, resp: httpx.Response) -> ProviderError:
        try:
            data = resp.json()
        except ValueError:
            data = None
        msg = _error_message(data) or resp.text[:200] or f"HTTP {resp.status_code}"
        code = resp.status_code
        if code == 404:
            return self._err("model_not_found", msg, status=code)
        if code == 429:
            return self._err(
                "rate_limited", msg, status=code, retry_after=self._retry_after(resp, data)
            )
        if code in (401, 403):
            return self._err("upstream_error", msg, status=code, retryable=False)
        if 400 <= code < 500:
            return self._err("invalid_request", msg, status=code)
        return self._err("upstream_error", msg, status=code)

    def _body(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        # `ChatRequest.messages`/`.tools`/`.tool_choice`/`.response_format` are already in this
        # wire shape (see the module docstring) — nothing to translate, unlike the Ollama adapter.
        body: dict[str, Any] = {"model": req.model, "messages": req.messages, "stream": stream}
        for key, value in (
            ("temperature", req.temperature),
            ("top_p", req.top_p),
            ("max_tokens", req.max_tokens),
            ("stop", req.stop),
            ("seed", req.seed),
            ("tools", req.tools),
            ("tool_choice", req.tool_choice),
            ("response_format", req.response_format),
        ):
            if value is not None:
                body[key] = value
        if stream and self.quirks.send_stream_options:
            body["stream_options"] = {"include_usage": True}
        return body

    # ── chat ──────────────────────────────────────────────────────────────────

    async def chat(self, req: ChatRequest) -> ChatResult:
        started = time.perf_counter()
        try:
            async with self._http() as client:
                resp = await client.post(
                    "/chat/completions", json=self._body(req, stream=False), headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise self._from_exception(exc) from exc
        if resp.status_code >= 400:
            raise self._from_response(resp)
        try:
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
        except (ValueError, AttributeError, IndexError) as exc:
            raise self._err("upstream_error", f"malformed response from {self.base_url}") from exc
        calls = _normalise_tool_calls(msg.get("tool_calls"))
        usage = data.get("usage") or {}
        total_ms = (time.perf_counter() - started) * 1000.0
        return ChatResult(
            text=str(msg.get("content") or ""),
            # The upstream's own report of which model actually answered — see the module
            # docstring: this is a passed-through claim, most relevant behind a router alias
            # like `auto`, never re-derived or verified against what was requested.
            model=str(data.get("model") or req.model),
            provider=self.name,
            tool_calls=calls,
            finish_reason=str(choice.get("finish_reason") or _finish(calls)),
            usage=Usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                estimated=not usage,
            ),
            total_ms=total_ms,
        )

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        started = time.perf_counter()
        committed = False
        try:
            async with (
                self._http() as client,
                client.stream(
                    "POST",
                    "/chat/completions",
                    json=self._body(req, stream=True),
                    headers=self._headers(),
                ) as resp,
            ):
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._from_response(resp)
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue  # SSE comments/keep-alives and non-data lines are not payloads
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except ValueError as exc:
                        raise self._err(
                            "stream_interrupted" if committed else "upstream_error",
                            "malformed stream event",
                            retryable=not committed,
                        ) from exc
                    if "error" in data:
                        raise self._err(
                            "stream_interrupted" if committed else "upstream_error",
                            _error_message(data),
                            retryable=not committed,
                        )
                    choices = data.get("choices") or []
                    delta = choices[0].get("delta") or {} if choices else {}
                    calls = _normalise_tool_calls(delta.get("tool_calls"))
                    chunk = ChatChunk(text=str(delta.get("content") or ""), tool_calls=calls)
                    if not committed and (chunk.text or calls):
                        committed = True
                        chunk.ttft_ms = (time.perf_counter() - started) * 1000.0
                    finish_reason = choices[0].get("finish_reason") if choices else None
                    if finish_reason:
                        chunk.finish_reason = str(finish_reason)
                    # ADR/spec "usage-in-last-chunk": a `stream_options.include_usage` reply's
                    # final event carries usage with an empty or absent `choices` — handled above
                    # by `if choices else {}` rather than a vendor-specific branch.
                    usage = data.get("usage")
                    if usage:
                        chunk.usage = Usage(
                            int(usage.get("prompt_tokens") or 0),
                            int(usage.get("completion_tokens") or 0),
                        )
                    yield chunk
        except httpx.HTTPError as exc:
            raise self._from_exception(exc, committed=committed) from exc

    # ── embeddings, discovery, health ─────────────────────────────────────────

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        try:
            async with self._http() as client:
                resp = await client.post(
                    "/embeddings", json={"model": model, "input": inputs}, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise self._from_exception(exc) from exc
        if resp.status_code >= 400:
            raise self._from_response(resp)
        try:
            data = resp.json()
        except (ValueError, AttributeError) as exc:
            raise self._err("upstream_error", f"malformed response from {self.base_url}") from exc
        items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        usage = data.get("usage") or {}
        return EmbedResult(
            vectors=[item.get("embedding", []) for item in items],
            model=str(data.get("model") or model),
            provider=self.name,
            usage=Usage(int(usage.get("prompt_tokens") or 0), estimated=not usage),
        )

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        resp = await client.get(path, headers=self._headers())
        if resp.status_code >= 400:
            raise self._from_response(resp)
        return resp.json()

    async def list_models(self) -> list[ModelInfo]:
        try:
            async with self._http() as client:
                data = await self._get_json(client, "/models")
        except httpx.HTTPError as exc:
            raise self._from_exception(exc) from exc
        infos: list[ModelInfo] = []
        for m in data.get("data") or []:
            name = str(m.get("id") or "")
            if not name:
                continue
            # A generic `/models` listing (the OpenAI shape) carries no capability info — assume
            # a plain chat model, the same conservative default OllamaProvider uses for a server
            # too old to report capabilities.
            infos.append(ModelInfo(name=name, capabilities=Capabilities()))
        return infos

    async def probe(self) -> ProbeResult:
        """Liveness only. Never raises: a probe that throws would break the health surface."""
        started = time.perf_counter()
        try:
            async with self._http() as client:
                await self._get_json(client, "/models")
        except (ProviderError, httpx.HTTPError, ValueError) as exc:
            detail = str(self._from_exception(exc) if isinstance(exc, httpx.HTTPError) else exc)
            return ProbeResult(False, (time.perf_counter() - started) * 1000.0, detail)
        return ProbeResult(True, (time.perf_counter() - started) * 1000.0)


def _normalise_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Already OpenAI-shaped on the wire — validated/defaulted, not translated (contrast with the
    Ollama adapter's version of this helper, which builds the shape from scratch).

    ``id`` is synthesised when a router omits one (found via the shared provider conformance
    suite, 2026-09-25: this used to fall back to ``""``, unlike the Ollama adapter's own synthetic
    ID — a caller correlating multiple tool calls in one response to their results in the next
    request cannot do so when every id is the same empty string). Real OpenAI and every
    spec-compliant server always sends one; this is defense-in-depth for the non-compliant routers
    this adapter's own docstring already exists to tolerate.
    """
    calls = []
    for c in raw or []:
        fn = c.get("function") or {}
        args = fn.get("arguments", "")
        if not isinstance(args, str):  # a lenient router that sent an object, not a JSON string
            args = json.dumps(args)
        calls.append(
            {
                "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": c.get("type") or "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            }
        )
    return calls

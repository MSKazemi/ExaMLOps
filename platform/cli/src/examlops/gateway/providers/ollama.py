"""Ollama provider — the native ``/api`` adapter (ADR 0152 d2).

Native rather than Ollama's OpenAI-compatible ``/v1``: that endpoint cannot set ``num_ctx`` or
``keep_alive``, which Skipper depends on, and it hides ``load_duration`` — the number that tells a
cold model load from a broken server. Shapes here were checked against a live Ollama 0.30.2.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from examlops.gateway.egress import validate_base_url
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

_NS_PER_MS = 1_000_000.0


def _finish(done_reason: str | None, tool_calls: list[dict[str, Any]]) -> str:
    if tool_calls:
        return "tool_calls"
    return "length" if done_reason == "length" else "stop"


def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI chat messages → Ollama's: content parts flattened, images pulled out as base64."""
    out: list[dict[str, Any]] = []
    for m in messages:
        msg: dict[str, Any] = {"role": m.get("role", "user")}
        content = m.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            images: list[str] = []
            for part in content:
                kind = part.get("type")
                if kind in ("text", "input_text"):
                    texts.append(str(part.get("text", "")))
                elif kind == "image_url":
                    ref = part.get("image_url")
                    url = ref.get("url") if isinstance(ref, dict) else ref
                    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                        images.append(url.split(";base64,", 1)[1])
                    else:
                        raise ProviderError(
                            "invalid_request", "image parts must be base64 data URLs for Ollama"
                        )
            msg["content"] = "\n".join(texts)
            if images:
                msg["images"] = images
        else:
            msg["content"] = "" if content is None else str(content)
        if m.get("tool_calls"):
            msg["tool_calls"] = [
                {"function": {"name": c["function"]["name"], "arguments": _args_obj(c["function"])}}
                for c in m["tool_calls"]
            ]
        if m.get("role") == "tool" and m.get("name"):
            msg["tool_name"] = m["name"]
        out.append(msg)
    return out


def _args_obj(fn: dict[str, Any]) -> dict[str, Any]:
    raw = fn.get("arguments", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _normalise_tool_calls(raw: Any) -> list[dict[str, Any]]:
    calls = []
    for c in raw or []:
        fn = c.get("function", {})
        calls.append(
            {
                "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": json.dumps(_args_obj(fn))},
            }
        )
    return calls


def _capabilities(names: list[str] | None, context_window: int | None = None) -> Capabilities:
    if not names:  # an older Ollama that reports none: assume a plain chat model
        return Capabilities(context_window=context_window)
    return Capabilities(
        chat="completion" in names,
        embeddings="embedding" in names,
        tools="tools" in names,
        vision="vision" in names,
        thinking="thinking" in names,
        context_window=context_window,
    )


class OllamaProvider:
    """One Ollama server (or the relay in front of it)."""

    type = "ollama"
    constrains_schema = True  # ``format`` takes a JSON schema and constrains decoding

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        locality: str = "local",
        keep_alive: str | int | None = None,
        options: dict[str, Any] | None = None,
        think: bool | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.locality = locality
        self.base_url = validate_base_url(base_url, locality=locality, allowed_hosts=allowed_hosts)
        self.keep_alive = keep_alive
        self.options = dict(options or {})
        self.think = think
        # Connect fails fast (a dead relay must not hold a request for minutes); the read timeout
        # is generous because the first request to a cold model includes its load (ADR 0153 d7).
        self.timeout = timeout or httpx.Timeout(300.0, connect=3.0)
        self._transport = transport
        self._shared = client

    # ── plumbing ──────────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """A client bound to *this* event loop. A caller-supplied shared client is used as-is."""
        if self._shared is not None:
            yield self._shared
            return
        # trust_env=False: proxy variables in the environment must not silently reroute prompts.
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, transport=self._transport, trust_env=False
        ) as client:
            yield client

    def _err(self, kind: str, message: str, **kw: Any) -> ProviderError:
        return ProviderError(kind, message, provider=self.name, **kw)

    def _from_exception(self, exc: Exception, *, committed: bool = False) -> ProviderError:
        """Classify a transport failure. ``committed``: bytes were already streamed to the caller."""
        if committed:  # ADR 0153 d9 — after the first byte the stream is over, never re-routed
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

    def _from_response(self, resp: httpx.Response) -> ProviderError:
        try:
            msg = str(resp.json().get("error", ""))
        except (ValueError, AttributeError):
            msg = ""
        msg = msg or resp.text[:200] or f"HTTP {resp.status_code}"
        code = resp.status_code
        if code == 404 and "not found" in msg.lower():
            return self._err("model_not_found", msg, status=code)
        if code == 429:
            retry_after = None
            with contextlib.suppress(ValueError):
                retry_after = float(resp.headers.get("Retry-After", ""))
            return self._err("rate_limited", msg, status=code, retry_after=retry_after)
        if code in (401, 403, 404):
            return self._err("upstream_error", msg, status=code, retryable=False)
        if 400 <= code < 500:
            return self._err("invalid_request", msg, status=code)
        return self._err("upstream_error", msg, status=code)

    def _body(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        options = dict(self.options)
        if "num_ctx" in req.extra:
            options["num_ctx"] = req.extra["num_ctx"]
        for key, value in (
            ("temperature", req.temperature),
            ("top_p", req.top_p),
            ("num_predict", req.max_tokens),
            ("stop", req.stop),
            ("seed", req.seed),
        ):
            if value is not None:
                options[key] = value
        body: dict[str, Any] = {
            "model": req.model,
            "messages": _convert_messages(req.messages),
            "stream": stream,
        }
        if options:
            body["options"] = options
        keep_alive = req.extra.get("keep_alive", self.keep_alive)
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        think = req.extra.get("think", self.think)
        if think is not None:
            body["think"] = think
        if req.tools:
            body["tools"] = req.tools
        fmt = req.response_format or {}
        if fmt.get("type") == "json_schema":
            body["format"] = (fmt.get("json_schema") or {}).get("schema", "json")
        elif fmt.get("type") == "json_object":
            body["format"] = "json"
        return body

    # ── chat ──────────────────────────────────────────────────────────────────

    async def chat(self, req: ChatRequest) -> ChatResult:
        started = time.perf_counter()
        try:
            async with self._http() as client:
                resp = await client.post("/api/chat", json=self._body(req, stream=False))
        except httpx.HTTPError as exc:
            raise self._from_exception(exc) from exc
        if resp.status_code >= 400:
            raise self._from_response(resp)
        try:
            data = resp.json()
            msg = data.get("message") or {}
        except (ValueError, AttributeError) as exc:
            raise self._err("upstream_error", f"malformed response from {self.base_url}") from exc
        calls = _normalise_tool_calls(msg.get("tool_calls"))
        total_ms = (time.perf_counter() - started) * 1000.0
        return ChatResult(
            text=str(msg.get("content") or ""),
            model=req.model,
            provider=self.name,
            reasoning=str(msg.get("thinking") or ""),
            tool_calls=calls,
            finish_reason=_finish(data.get("done_reason"), calls),
            usage=Usage(
                int(data.get("prompt_eval_count") or 0),
                int(data.get("eval_count") or 0),
                estimated="eval_count" not in data,
            ),
            ttft_ms=total_ms,
            load_ms=float(data.get("load_duration") or 0) / _NS_PER_MS,
            total_ms=total_ms,
        )

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        started = time.perf_counter()
        committed = False
        try:
            async with (
                self._http() as client,
                client.stream("POST", "/api/chat", json=self._body(req, stream=True)) as resp,
            ):
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._from_response(resp)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError as exc:
                        raise self._err(
                            "stream_interrupted" if committed else "upstream_error",
                            "malformed stream line",
                            retryable=not committed,
                        ) from exc
                    if "error" in data:
                        raise self._err(
                            "stream_interrupted" if committed else "upstream_error",
                            str(data["error"]),
                            retryable=not committed,
                        )
                    msg = data.get("message") or {}
                    calls = _normalise_tool_calls(msg.get("tool_calls"))
                    chunk = ChatChunk(
                        text=str(msg.get("content") or ""),
                        reasoning=str(msg.get("thinking") or ""),
                        tool_calls=calls,
                    )
                    if not committed and (chunk.text or chunk.reasoning or calls):
                        committed = True
                        chunk.ttft_ms = (time.perf_counter() - started) * 1000.0
                    if data.get("done"):
                        chunk.finish_reason = _finish(data.get("done_reason"), calls)
                        chunk.usage = Usage(
                            int(data.get("prompt_eval_count") or 0),
                            int(data.get("eval_count") or 0),
                            estimated="eval_count" not in data,
                        )
                        chunk.load_ms = float(data.get("load_duration") or 0) / _NS_PER_MS
                    yield chunk
        except httpx.HTTPError as exc:
            raise self._from_exception(exc, committed=committed) from exc

    # ── embeddings, discovery, health ─────────────────────────────────────────

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        try:
            async with self._http() as client:
                resp = await client.post("/api/embed", json={"model": model, "input": inputs})
        except httpx.HTTPError as exc:
            raise self._from_exception(exc) from exc
        if resp.status_code >= 400:
            raise self._from_response(resp)
        data = resp.json()
        return EmbedResult(
            vectors=data.get("embeddings", []),
            model=model,
            provider=self.name,
            usage=Usage(int(data.get("prompt_eval_count") or 0)),
        )

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        resp = await client.get(path)
        if resp.status_code >= 400:
            raise self._from_response(resp)
        return resp.json()

    async def _resident(self, client: httpx.AsyncClient) -> list[str]:
        try:
            data = await self._get_json(client, "/api/ps")
        except (ProviderError, httpx.HTTPError, ValueError):
            return []  # residency is advisory; its absence must not fail discovery or a probe
        return [str(m.get("name") or m.get("model")) for m in data.get("models", [])]

    async def list_models(self) -> list[ModelInfo]:
        try:
            async with self._http() as client:
                tags = await self._get_json(client, "/api/tags")
                resident = set(await self._resident(client))
                infos: list[ModelInfo] = []
                for m in tags.get("models", []):
                    name = str(m.get("name") or m.get("model"))
                    ctx: int | None = None
                    caps = m.get("capabilities")
                    if caps is None:  # older server: ask /api/show for capabilities + window
                        try:
                            resp = await client.post("/api/show", json={"model": name})
                            shown = resp.json() if resp.status_code < 400 else {}
                        except (httpx.HTTPError, ValueError):
                            shown = {}
                        caps = shown.get("capabilities")
                        for key, value in (shown.get("model_info") or {}).items():
                            if key.endswith("context_length") and isinstance(value, int):
                                ctx = value
                    infos.append(
                        ModelInfo(
                            name=name,
                            capabilities=_capabilities(caps, ctx),
                            size_bytes=m.get("size"),
                            resident=name in resident,
                        )
                    )
                return infos
        except httpx.HTTPError as exc:
            raise self._from_exception(exc) from exc

    async def probe(self) -> ProbeResult:
        """Liveness + residency. Never raises: a probe that throws would break the health surface."""
        started = time.perf_counter()
        try:
            async with self._http() as client:
                await self._get_json(client, "/api/tags")
                resident = await self._resident(client)
        except (ProviderError, httpx.HTTPError, ValueError) as exc:
            detail = str(self._from_exception(exc) if isinstance(exc, httpx.HTTPError) else exc)
            return ProbeResult(False, (time.perf_counter() - started) * 1000.0, detail)
        return ProbeResult(True, (time.perf_counter() - started) * 1000.0, resident=resident)

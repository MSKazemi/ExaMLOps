"""Track V (R-V1/R-V2) — ``VLLMServerEngine``: a client of a running ``vllm serve``.

This is the **production** vLLM engine. The in-process ``VLLMEngine`` (kept as
``vllm-inproc``) drives vLLM's *offline batch* API, which is the right tool for scoring a
fixed corpus and the wrong one for serving: continuous batching schedules concurrent
in-flight requests, and an embedded ``LLM`` object inside a short-lived CLI process has
exactly one caller, exposes no ``/metrics``, and reloads the weights per process.

Talking to the server instead buys four things at once (ADR 0107):

* **continuous batching** across every concurrent client, not just this one;
* **token-true streaming** by parsing SSE frames — no ``AsyncLLMEngine``, no event loop
  forced into a synchronous CLI;
* a **real readiness probe** (``GET /health``) instead of an ``is not None`` attribute check;
* the ``vllm:*`` **Prometheus metrics** the platform's existing monitoring stack scrapes.

Pure stdlib (``urllib``) — no new runtime dependency, and nothing here needs a GPU, so the
whole class is exercisable in CI against a stub HTTP server.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from examlops.engines.config import (
    Completion,
    EngineConfig,
    _sampling_kwargs,
    reasoning_tokens_from_usage,
    schema_response_format,
)
from examlops.engines.media import flatten_messages, normalize_content

_DEFAULT_TIMEOUT = float(os.getenv("EXAMLOPS_VLLM_TIMEOUT", "120"))
_DEFAULT_CONNECT_TIMEOUT = float(os.getenv("EXAMLOPS_VLLM_CONNECT_TIMEOUT", "10"))


class EngineUnreachable(RuntimeError):
    """The endpoint could not be reached or returned an error status."""


class VLLMServerEngine:
    """OpenAI-compatible client of a running vLLM server.

    ``base_url`` points at the server root (e.g. ``http://gpu-node-03:8000``); the
    ``/v1`` prefix is added here so callers configure one address, not two.
    """

    #: This engine can hold the model to a JSON schema while it decodes (ADR 0035 clause 1),
    #: so the gateway hands it the schema instead of only checking the answer afterwards.
    constrains_schema = True

    name = "vllm-server"

    def __init__(
        self,
        base_url: str,
        model: str,
        config: EngineConfig | None = None,
        *,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.config = config or EngineConfig(engine="vllm-server")
        self.timeout = timeout
        self._api_key = api_key
        self._ready = False

    # ── auth ──────────────────────────────────────────────────────────────────

    def _token(self) -> str:
        """Resolve the API key lazily via D7 secrets; never cached to disk or logged."""
        if self._api_key is not None:
            return self._api_key
        ref = self.config.api_key_secret_ref
        if ref:
            try:
                from examlops import secrets as _secrets

                self._api_key = _secrets.get_secret(ref)
                return self._api_key
            except Exception:
                # A missing secret must not hard-fail an unauthenticated local endpoint.
                pass
        self._api_key = os.getenv("EXAMLOPS_VLLM_API_KEY", "")
        return self._api_key

    def _headers(self, *, stream: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # ADR 0148 d1: one trace from agent to tensor — carry W3C trace context to the server.
        from examlops.telemetry.propagation import inject_http_headers

        inject_http_headers(headers)
        return headers

    # ── transport ─────────────────────────────────────────────────────────────

    def _request(self, path: str, body: dict[str, Any] | None = None, *, stream: bool = False):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        method = "POST" if body is not None else "GET"
        req = urllib.request.Request(
            url, data=data, headers=self._headers(stream=stream), method=method
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:500]
            except Exception:
                pass
            raise EngineUnreachable(f"{url} → HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EngineUnreachable(f"{url} unreachable: {exc}") from exc

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._request(path, body) as resp:
            return json.loads(resp.read().decode())

    # ── payload ───────────────────────────────────────────────────────────────

    @property
    def reasoning_cap_param(self) -> str | None:
        return self.config.reasoning_cap_param

    def _payload(self, messages: list[dict[str, Any]], kw: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.served_model_name or self.model,
            "messages": messages,
        }
        body.update(_sampling_kwargs(kw))
        schema = kw.get("response_schema")
        if schema is not None:  # ADR 0035 clause 1: constrain the decoder, do not ask nicely
            body["response_format"] = schema_response_format(schema)
        # ADR 0035 clause 2: a thinking cap goes on the wire only under the field name the
        # operator declared this server accepts. No declared name, nothing sent.
        cap = kw.get("reasoning_budget")
        if cap is not None and self.reasoning_cap_param:
            body[self.reasoning_cap_param] = int(cap)
        # ADR 0143 d9: an agent tool step is constrained to `required`/named, never `auto`.
        from examlops.engines.tool_policy import apply_tool_choice_policy

        apply_tool_choice_policy(
            body,
            tools=kw.get("tools"),
            tool_choice=kw.get("tool_choice"),
            tool_step=bool(kw.get("tool_step")),
        )
        return body

    # ── chat (R-V3: media parts forwarded verbatim) ───────────────────────────

    def chat(self, messages: list[dict[str, Any]], **kw: Any) -> Completion:
        messages, stats = normalize_content(messages, self.config)
        started = time.monotonic()
        payload = self._payload(messages, kw)
        data = self._post_json("/v1/chat/completions", payload)
        self._ready = True
        comp = _completion_from_response(data, stats.images, time.monotonic() - started)
        if kw.get("tool_step"):  # ADR 0143 d9: parse-failure rate is a tracked metric
            from examlops.engines.tool_policy import record_tool_calls

            record_tool_calls(comp.tool_calls)
        return comp

    def chat_stream(self, messages: list[dict[str, Any]], **kw: Any) -> Iterator[str]:
        """Yield token-true deltas from the server's SSE stream (R-V2)."""
        messages, _ = normalize_content(messages, self.config)
        payload = self._payload(messages, kw)
        payload["stream"] = True
        yield from self._stream_sse("/v1/chat/completions", payload)

    def _stream_sse(self, path: str, payload: dict[str, Any]) -> Iterator[str]:
        started = time.monotonic()
        self.last_ttft_s = 0.0
        with self._request(path, payload, stream=True) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue  # a malformed frame must not abort a live stream
                delta = _delta_text(event)
                if not delta:
                    continue
                if not self.last_ttft_s:
                    self.last_ttft_s = time.monotonic() - started
                    self._ready = True
                yield delta

    # ── prompt-shaped surface (back-compat with InferenceEngine) ──────────────

    def generate(self, prompt: str, **kw: Any) -> Completion:
        return self.chat([{"role": "user", "content": prompt}], **kw)

    def stream(self, prompt: str, **kw: Any) -> Iterator[str]:
        yield from self.chat_stream([{"role": "user", "content": prompt}], **kw)

    # ── introspection ─────────────────────────────────────────────────────────

    def health(self) -> bool:
        """Real readiness (R-A3/R-V1). Never raises — a probe must not break a health surface."""
        try:
            with self._request("/health") as resp:
                self._ready = 200 <= resp.status < 300
        except Exception:
            self._ready = False
        return self._ready

    def models(self) -> list[str]:
        try:
            with self._request("/v1/models") as resp:
                data = json.loads(resp.read().decode())
            return [str(m.get("id")) for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    def metrics(self) -> dict[str, float]:
        """Scrape the server's ``vllm:*`` gauges/counters (TTFT, KV-cache usage, queue depth).

        Returns the *sum* of each series across labels — enough for `exa serve llm status`;
        the real time-series analysis belongs to Prometheus, which scrapes the same endpoint.
        """
        try:
            with self._request("/metrics") as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            return {}
        return parse_prometheus_text(text)


def parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse the Prometheus exposition format, keeping only ``vllm:*`` series.

    Pure function so it is testable without a server. Sample lines are summed per metric
    name (labels dropped), and ``_bucket`` series are skipped — a histogram bucket sum is
    meaningless and would be misread as a value.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith("vllm:"):
            continue
        name, _, rest = line.partition("{")
        if rest:
            _, _, value_part = rest.partition("}")
        else:
            name, _, value_part = line.partition(" ")
        name = name.strip()
        if name.endswith("_bucket"):
            continue
        try:
            value = float(value_part.strip().split()[0])
        except (ValueError, IndexError):
            continue
        out[name] = out.get(name, 0.0) + value
    return out


# ── response mapping ──────────────────────────────────────────────────────────


def _delta_text(event: dict[str, Any]) -> str:
    choices = event.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    # Some servers stream `text` (completions API shape) rather than a chat delta.
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def _completion_from_response(data: dict[str, Any], image_count: int, elapsed: float) -> Completion:
    choices = data.get("choices") or [{}]
    message = choices[0].get("message") or {}
    text = message.get("content") or choices[0].get("text") or ""
    usage = data.get("usage") or {}
    trace = message.get("reasoning_content") or message.get("reasoning")
    calls = message.get("tool_calls")
    return Completion(
        tool_calls=calls if isinstance(calls, list) and calls else None,
        reasoning_tokens=reasoning_tokens_from_usage(usage),
        reasoning_text=trace if isinstance(trace, str) and trace else None,
        text=str(text),
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        finish_reason=str(choices[0].get("finish_reason") or "stop"),
        total_s=elapsed,
        image_count=image_count,
    )


def chat_via_generate(engine: Any, messages: list[dict[str, Any]], **kw: Any) -> Completion:
    """Default ``chat`` for a text-only engine: flatten, then warn about dropped media (R-V4).

    Lives here rather than on each engine so ``EchoEngine`` and ``VLLMEngine`` gain a chat
    surface without either of them learning about modality.
    """
    import warnings

    prompt, dropped = flatten_messages(messages)
    if dropped:
        detail = ", ".join(f"{n} {kind}" for kind, n in sorted(dropped.items()))
        warnings.warn(
            f"engine '{getattr(engine, 'name', '?')}' has no chat surface; dropped {detail} "
            "part(s) from the request. Use a chat-capable engine (vllm-server) to serve "
            "multimodal input.",
            RuntimeWarning,
            stacklevel=3,
        )
    comp = engine.generate(prompt, **kw)
    return comp

"""Bridge an async :class:`Provider` into the existing synchronous gateway (ADR 0152 d2, d6).

``GatewayClient`` and ``Router`` take ``Backend`` callables. Wrapping a provider as one makes it a
first-class route today — with the client's keys, budgets, guardrails, cache, cost and spans applied
around it — without touching that code path. The service (P2) uses the providers directly.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from examlops.gateway.providers.base import ChatRequest, Provider, run_sync

logger = logging.getLogger(__name__)


def _request(model: str, messages: list[dict[str, Any]], kw: dict[str, Any]) -> ChatRequest:
    schema = kw.get("response_schema")
    response_format = kw.get("response_format")
    if schema is not None:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": schema},
        }
    stop = kw.get("stop")
    return ChatRequest(
        model=model,
        messages=messages,
        temperature=kw.get("temperature"),
        top_p=kw.get("top_p"),
        max_tokens=kw.get("max_tokens"),
        stop=[stop] if isinstance(stop, str) else stop,
        seed=kw.get("seed"),
        tools=kw.get("tools"),
        tool_choice=kw.get("tool_choice"),
        response_format=response_format,
        extra=dict(kw.get("ollama") or {}),
    )


def provider_backend(provider: Provider, *, upstream_model: str | None = None):
    """A sync gateway ``Backend`` that serves requests from ``provider``.

    ``upstream_model`` is the model name sent upstream when it differs from the route's name.
    The callable carries ``.health`` (used by ``GatewayClient.health``) and ``constrains_schema``.
    """
    from examlops.gateway import Completion

    def _backend(model: str, messages: list[dict[str, Any]], **kw: Any) -> Completion:
        result = run_sync(provider.chat(_request(upstream_model or model, messages, kw)))
        return Completion(
            text=result.text,
            model=model,
            backend=provider.name,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
        )

    _backend.health = lambda: run_sync(provider.probe()).ok  # type: ignore[attr-defined]
    _backend.provider = provider  # type: ignore[attr-defined]
    _backend.constrains_schema = bool(  # type: ignore[attr-defined]
        getattr(provider, "constrains_schema", False)
    )
    return _backend


def add_provider_routes(
    router: Any, provider: Provider, *, only: set[str] | None = None
) -> list[str]:
    """Route every chat-capable model the provider reports under its own name; return the names.

    Discovery failure is a warning and adds nothing: an unreachable Ollama must not stop the gateway
    from serving the routes it already has — and the reason is logged, not swallowed.
    """
    try:
        models = run_sync(provider.list_models())
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort by design
        logger.warning(
            "provider %s: model discovery failed (%s) — no routes added", provider.name, exc
        )
        return []
    added: list[str] = []
    for info in models:
        if not info.capabilities.chat or (only is not None and info.name not in only):
            continue
        router.add_route(info.name, [(provider.name, provider_backend(provider))])
        added.append(info.name)
    return added


def ollama_from_env() -> Provider | None:
    """The provider described by ``EXAMLOPS_LLM_OLLAMA_URL``, or ``None`` when it is unset."""
    url = os.getenv("EXAMLOPS_LLM_OLLAMA_URL", "").strip()
    if not url:
        return None
    from examlops.gateway.providers.ollama import OllamaProvider

    options: dict[str, Any] = {}
    if os.getenv("AGENT_OLLAMA_NUM_CTX"):
        options["num_ctx"] = int(os.environ["AGENT_OLLAMA_NUM_CTX"])
    return OllamaProvider(
        os.getenv("EXAMLOPS_LLM_OLLAMA_NAME", "ollama"),
        url,
        keep_alive=os.getenv("AGENT_OLLAMA_KEEP_ALIVE") or None,
        options=options,
    )

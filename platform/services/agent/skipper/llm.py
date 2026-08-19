from __future__ import annotations

import httpx

from skipper import config


def build_llm(model: str | None = None):
    """Build the LLM backend.

    Preference order: Azure OpenAI / Foundry (AZURE_OPENAI_API_KEY +
    AZURE_OPENAI_ENDPOINT) → Claude API (ANTHROPIC_API_KEY) → Ollama. Claude
    uses adaptive thinking so the model decides when to reason step-by-step.
    """
    if config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT:
        from langchain_openai import ChatOpenAI

        # The Foundry v1 endpoint is OpenAI-compatible: base_url + api_key, with
        # the deployment name used as the model id. Temperature is left unset —
        # gpt-5.x reasoning models reject anything other than the default.
        # api_key/model are accepted as plain str at runtime (pydantic coerces);
        # the stub types api_key as SecretStr, hence the ignore.
        return ChatOpenAI(
            model=model or config.AZURE_OPENAI_DEPLOYMENT,
            base_url=config.AZURE_OPENAI_ENDPOINT,
            api_key=config.AZURE_OPENAI_API_KEY,  # type: ignore[arg-type]
        )
    if config.ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic

        # model/anthropic_api_key/max_tokens are valid ChatAnthropic pydantic
        # fields at runtime; the shipped stub omits them from __init__.
        return ChatAnthropic(  # type: ignore[call-arg]
            model=model or config.ANTHROPIC_MODEL,
            anthropic_api_key=config.ANTHROPIC_API_KEY,
            thinking={"type": "adaptive"},
            max_tokens=16000,
        )
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=model or config.AGENT_MODEL,
        base_url=config.AGENT_OLLAMA_URL,
        temperature=0,
        keep_alive=config.AGENT_OLLAMA_KEEP_ALIVE,
        reasoning=config.AGENT_OLLAMA_REASONING,
    )


# Statuses meaning "the endpoint is alive but refused our credential". A live
# gateway answering 401 cannot serve a single token, so it must report unhealthy:
# to an operator, a healthy-but-unusable backend is worse than an obviously dead
# one, because it sends them looking for the fault everywhere except the key.
_AUTH_REJECTED = frozenset({401, 403})


def _endpoint_reachable(url: str, *, timeout: float = 5.0, headers: dict | None = None) -> bool:
    """True if *url* answers within *timeout* and does not reject our credential.

    Three outcomes, two of them unhealthy:

    * transport error (DNS/connect/timeout) -> the endpoint is down;
    * ``401``/``403`` -> the endpoint is up but the key is invalid, revoked, or
      issued for a different resource, which is just as fatal for the agent;
    * anything else (``200``, ``404``, ``5xx``) -> routable, treated as reachable.
      A non-auth status is deliberately *not* fatal because providers expose
      different probe paths, and a ``404`` on ``/models`` says nothing about
      whether chat completions works.

    Verified against the live LXP deployment on 2026-08-20: the Foundry endpoint
    answered ``401`` to the stored key while ``check_backend()`` still reported
    ``ok: True``, so the agent advertised a backend it could not use at all.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers or {})
    except httpx.RequestError:
        return False
    return resp.status_code not in _AUTH_REJECTED


def check_backend() -> dict:
    """Return backend info dict: {ok, type, model}.

    Every backend is now actively probed for reachability. Previously Azure/Claude
    were assumed healthy from mere env-var presence, so a dead endpoint reported
    ``ok:True`` and masked the outage.
    """
    if config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT:
        # Probe /models *with* the key. Probing the bare endpoint unauthenticated
        # returned the same 401 whether or not a key was supplied, so it could
        # never distinguish a good key from a revoked one.
        ok = _endpoint_reachable(
            config.AZURE_OPENAI_ENDPOINT.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {config.AZURE_OPENAI_API_KEY}"},
        )
        return {"ok": ok, "type": "azure", "model": config.AZURE_OPENAI_DEPLOYMENT}
    if config.ANTHROPIC_API_KEY:
        ok = _endpoint_reachable(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        return {"ok": ok, "type": "claude", "model": config.ANTHROPIC_MODEL}
    ok = _endpoint_reachable(f"{config.AGENT_OLLAMA_URL}/api/tags")
    return {"ok": ok, "type": "ollama", "model": config.AGENT_MODEL}


def check_ollama() -> bool:
    """Backward-compatible shim used by legacy callers."""
    return check_backend()["ok"]

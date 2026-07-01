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


def _endpoint_reachable(url: str, *, timeout: float = 5.0, headers: dict | None = None) -> bool:
    """True if *url* answers any HTTP status (even 401/404) within *timeout*.

    Any HTTP response proves the endpoint is up and routable; only a transport
    error (DNS/connect/timeout) means it is genuinely unreachable. This lets a
    down Azure/Claude endpoint be reported unhealthy instead of assumed-OK.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            client.get(url, headers=headers or {})
        return True
    except httpx.RequestError:
        return False


def check_backend() -> dict:
    """Return backend info dict: {ok, type, model}.

    Every backend is now actively probed for reachability. Previously Azure/Claude
    were assumed healthy from mere env-var presence, so a dead endpoint reported
    ``ok:True`` and masked the outage.
    """
    if config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT:
        ok = _endpoint_reachable(config.AZURE_OPENAI_ENDPOINT)
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

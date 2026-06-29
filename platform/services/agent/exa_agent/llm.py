from __future__ import annotations

import httpx

from exa_agent import config


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
        return ChatOpenAI(
            model=model or config.AZURE_OPENAI_DEPLOYMENT,
            base_url=config.AZURE_OPENAI_ENDPOINT,
            api_key=config.AZURE_OPENAI_API_KEY,
        )
    if config.ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
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


def check_backend() -> dict:
    """Return backend info dict: {ok, type, model}."""
    if config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT:
        return {"ok": True, "type": "azure", "model": config.AZURE_OPENAI_DEPLOYMENT}
    if config.ANTHROPIC_API_KEY:
        return {"ok": True, "type": "claude", "model": config.ANTHROPIC_MODEL}
    try:
        with httpx.Client(timeout=5.0) as client:
            client.get(f"{config.AGENT_OLLAMA_URL}/api/tags")
        return {"ok": True, "type": "ollama", "model": config.AGENT_MODEL}
    except httpx.RequestError:
        return {"ok": False, "type": "ollama", "model": config.AGENT_MODEL}


def check_ollama() -> bool:
    """Backward-compatible shim used by legacy callers."""
    return check_backend()["ok"]

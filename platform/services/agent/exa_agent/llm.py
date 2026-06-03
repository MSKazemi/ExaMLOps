from __future__ import annotations

import httpx
from langchain_ollama import ChatOllama

from exa_agent import config


def build_llm(model: str | None = None) -> ChatOllama:
    return ChatOllama(
        model=model or config.AGENT_MODEL,
        base_url=config.AGENT_OLLAMA_URL,
        temperature=0,
    )


def check_ollama() -> bool:
    try:
        with httpx.Client(timeout=5.0) as client:
            client.get(f"{config.AGENT_OLLAMA_URL}/api/tags")
        return True
    except httpx.RequestError:
        return False

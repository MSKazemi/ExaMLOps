"""Gateway providers (ADR 0152): async adapters that talk to one upstream each."""

from examlops.gateway.providers.base import (
    ERROR_STATUS,
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChatResult,
    EmbedResult,
    ModelInfo,
    ProbeResult,
    Provider,
    ProviderError,
    Usage,
    run_sync,
)
from examlops.gateway.providers.bridge import (
    add_provider_routes,
    ollama_from_env,
    provider_backend,
)
from examlops.gateway.providers.ollama import OllamaProvider
from examlops.gateway.providers.openai_compat import OpenAICompatProvider, OpenAICompatQuirks

__all__ = [
    "ERROR_STATUS",
    "Capabilities",
    "ChatChunk",
    "ChatRequest",
    "ChatResult",
    "EmbedResult",
    "ModelInfo",
    "OllamaProvider",
    "OpenAICompatProvider",
    "OpenAICompatQuirks",
    "ProbeResult",
    "Provider",
    "ProviderError",
    "Usage",
    "add_provider_routes",
    "ollama_from_env",
    "provider_backend",
    "run_sync",
]

from __future__ import annotations

import os
from pathlib import Path

# exa_agent/config.py → parents: [0]=exa_agent [1]=agent [2]=services [3]=platform [4]=repo
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Azure OpenAI / AI Foundry backend (preferred when configured).
# AZURE_OPENAI_ENDPOINT is the Foundry "v1" project endpoint base URL, e.g.
#   https://<resource>.services.ai.azure.com/openai/v1/
# which is OpenAI-compatible, so we drive it with langchain-openai's ChatOpenAI
# (base_url + api_key) rather than AzureChatOpenAI (that one expects the classic
# https://<resource>.openai.azure.com shape). AZURE_OPENAI_DEPLOYMENT is the
# deployment name shown in Foundry (e.g. "gpt-5.4-mini"), used as the model id.
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini")

# Claude API backend
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Ollama backend (fallback when ANTHROPIC_API_KEY is unset)
AGENT_MODEL = os.getenv("AGENT_MODEL", "llama3.1:8b")
AGENT_OLLAMA_URL = os.getenv("AGENT_OLLAMA_URL", "http://localhost:11436")

# Ollama speed tuning — matters a lot on CPU-only servers (e.g. n1). keep_alive
# pins the model in memory so it is not reloaded (30-60s) between turns; reasoning
# disables "thinking" models' (qwen3) extra reasoning tokens for snappier replies.
# AGENT_OLLAMA_REASONING: "false" disables thinking (default), "true" forces it on,
# "default"/"none" leaves the model default.
AGENT_OLLAMA_KEEP_ALIVE = os.getenv("AGENT_OLLAMA_KEEP_ALIVE", "30m")
_reasoning = os.getenv("AGENT_OLLAMA_REASONING", "false").strip().lower()
AGENT_OLLAMA_REASONING = (
    True if _reasoning in ("true", "1", "yes", "on")
    else False if _reasoning in ("false", "0", "no", "off")
    else None
)

MLFLOW_URL = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000")
RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:18001")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:19090")
CONTROL_PLANE_URL = os.getenv("CONTROL_PLANE_URL", "http://localhost:18002")
CONTROL_PLANE_TOKEN = os.getenv("CONTROL_PLANE_TOKEN", "")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:18099")
DASHBOARD_ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")

AGENT_DB = os.getenv("AGENT_DB", "./agent_memory.db")
AGENT_DOCS_ROOT = os.getenv("AGENT_DOCS_ROOT", str(_REPO_ROOT / "docs"))
CLAUDE_MD = os.getenv("AGENT_CLAUDE_MD", str(_REPO_ROOT / "CLAUDE.md"))

HTTP_TIMEOUT = float(os.getenv("AGENT_HTTP_TIMEOUT", "10.0"))

from __future__ import annotations

import os
from pathlib import Path

# exa_agent/config.py → parents: [0]=exa_agent [1]=agent [2]=services [3]=platform [4]=repo
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Claude API backend (preferred)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Ollama backend (fallback when ANTHROPIC_API_KEY is unset)
AGENT_MODEL = os.getenv("AGENT_MODEL", "llama3.1:8b")
AGENT_OLLAMA_URL = os.getenv("AGENT_OLLAMA_URL", "http://localhost:11436")

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

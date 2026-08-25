from __future__ import annotations

import os
from pathlib import Path

# skipper/config.py → parents: [0]=skipper [1]=agent [2]=services [3]=platform [4]=repo
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Azure OpenAI / AI Foundry backend (preferred when configured).
# AZURE_OPENAI_ENDPOINT is the Foundry "v1" project endpoint base URL, e.g.
#   https://<resource>.services.ai.azure.com/openai/v1/
# which is OpenAI-compatible, so we drive it with langchain-openai's ChatOpenAI
# (base_url + api_key) rather than AzureChatOpenAI (that one expects the classic
# https://<resource>.openai.azure.com shape). AZURE_OPENAI_DEPLOYMENT is the
# deployment name shown in Foundry (e.g. "gpt-5.5"), used as the model id.
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5")

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
    True
    if _reasoning in ("true", "1", "yes", "on")
    else False
    if _reasoning in ("false", "0", "no", "off")
    else None
)

MLFLOW_URL = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000")
RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:18001")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:19090")
CONTROL_PLANE_URL = os.getenv("CONTROL_PLANE_URL", "http://localhost:18002")
CONTROL_PLANE_TOKEN = os.getenv("CONTROL_PLANE_TOKEN", "")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:18099")
DASHBOARD_ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")

# Optional credential gating the OpenAI-compatible bridge, agent status/history APIs, and
# WebSocket tools. Unset means open access on the loopback-only development default.
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

AGENT_DB = os.getenv("AGENT_DB", "./agent_memory.db")
AGENT_DOCS_ROOT = os.getenv("AGENT_DOCS_ROOT", str(_REPO_ROOT / "docs"))

# Knowledge / Docs-RAG memory tier (T2, Phase 3, ADR 0101). Chunk+embed the docs so the agent
# answers "how do I …?" from the actual documentation with citations, reusing the platform's
# examlops.vector_store seam driven by Skipper's local embeddings. Degrades to the ripgrep docs
# tool when embeddings/vector-store are unavailable — never worse than today.
AGENT_KNOWLEDGE_ENABLED = os.getenv("AGENT_KNOWLEDGE_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
AGENT_KNOWLEDGE_KB = os.getenv("AGENT_KNOWLEDGE_KB", "skipper-knowledge")
# Semicolon-separated public documentation roots to ingest. Absolute or repo-relative. Only
# Markdown files are indexed. Private design notes and assistant working files are deliberately
# excluded from the default knowledge surface.
AGENT_KNOWLEDGE_ROOTS = os.getenv(
    "AGENT_KNOWLEDGE_ROOTS",
    str(_REPO_ROOT / "docs"),
)
AGENT_KNOWLEDGE_CHUNK_SIZE = int(os.getenv("AGENT_KNOWLEDGE_CHUNK_SIZE", "60"))
AGENT_KNOWLEDGE_OVERLAP = int(os.getenv("AGENT_KNOWLEDGE_OVERLAP", "15"))


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "on")


# --- Long-term (cross-thread) memory — SM1 substrate (see design/adr/0033) ---
# Additive: if the store or the embedding backend is unavailable, the agent runs
# with short-term (per-thread checkpoint) memory only. The store lives in its OWN
# sqlite file, separate from both platform.db and the checkpointer DB. Uses the
# sync SqliteStore (sqlite-vec) to match the sync-graph-in-threadpool server.
AGENT_MEMORY_ENABLED = _env_bool("AGENT_MEMORY_ENABLED", True)
AGENT_MEMORY_DB = os.getenv("AGENT_MEMORY_DB", "./skipper_memory.db")
# Local embeddings only (no cloud). "ollama" uses AGENT_OLLAMA_URL;
# "sentence-transformers" runs fully in-process/offline. AGENT_EMBED_DIMS MUST
# match the model: nomic-embed-text=768, bge-m3=1024, all-MiniLM-L6-v2=384.
AGENT_EMBED_BACKEND = os.getenv("AGENT_EMBED_BACKEND", "ollama").strip().lower()
AGENT_EMBED_MODEL = os.getenv("AGENT_EMBED_MODEL", "nomic-embed-text")
AGENT_EMBED_DIMS = int(os.getenv("AGENT_EMBED_DIMS", "768"))
# Context trimming for long threads. SM1 ships the plumbing (langchain-core
# trim_messages, ephemeral via llm_input_messages); a full LangMem running-summary
# is a follow-up. Off by default to leave the ReAct tool-loop boundaries untouched
# until validated on the small local models.
AGENT_SUMMARIZE_ENABLED = _env_bool("AGENT_SUMMARIZE_ENABLED", False)
AGENT_MAX_CONTEXT_TOKENS = int(os.getenv("AGENT_MAX_CONTEXT_TOKENS", "12000"))
# Actor written into preference memory + memory audit events. Mirrors the
# platform's EXAMLOPS_ACTOR convention.
AGENT_ACTOR = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "operator"
# SM3 governance: gate durable procedure writes behind operator confirmation (HITL),
# and audit every memory mutation to platform_db.audit_events. Both default on.
AGENT_MEMORY_REQUIRE_CONFIRM = _env_bool("AGENT_MEMORY_REQUIRE_CONFIRM", True)
AGENT_MEMORY_AUDIT = _env_bool("AGENT_MEMORY_AUDIT", True)
# SM3 review-queue (BL-009): when enabled, record_procedure ENQUEUES the write for batch
# operator review (list → approve/reject via `python -m skipper.memory_admin review …`)
# instead of the inline HITL interrupt. Off by default — inline HITL stays the default path.
AGENT_MEMORY_REVIEW_QUEUE = _env_bool("AGENT_MEMORY_REVIEW_QUEUE", False)
AGENT_MEMORY_REVIEW_DB = os.getenv("AGENT_MEMORY_REVIEW_DB", "./skipper_review.db")
# BL-007: source the agent's platform-capability tools from the shared examlops.mcp registry
# (single source of truth) instead of the in-repo duplicates. Off by default. Mutating MCP tools
# remain gated by EXAMLOPS_MCP_ALLOW_WRITES.
# Phase 5 (ADR 0102): now that the MCP registry has full read coverage + gated/tiered writes and
# the bridge HITL-wraps mutating tools, the single-agent fallback path sources tools from MCP by
# default. (The supervisor path — the default topology — already draws reads from MCP + writes
# from the in-repo set regardless of this flag.)
AGENT_USE_MCP_TOOLS = _env_bool("AGENT_USE_MCP_TOOLS", True)

# Reasoning topology (Phase 4, ADR 0100). `auto` builds the supervisor graph — a deterministic
# router dispatches each turn to one of the specialist ReAct sub-agents (manager/monitor/helper/
# finops/governor/general), each bound to a scoped tool pack so a local 8B model only sees the
# ~10-20 relevant tools. `single` forces the legacy single ReAct agent. Any build failure in
# `auto` degrades to `single` (additive, never breaks the chat).
AGENT_SUPERVISOR_MODE = os.getenv("AGENT_SUPERVISOR_MODE", "auto").strip().lower()

# Self-instrumentation (Phase 2, ADR 0103): record each turn's tool calls into the shared
# examlops.agentops tables (agent_sessions/agent_tool_calls) so tool_success_rate becomes real,
# and abort a runaway turn in-loop with the AgentCircuitBreaker. Both fail open (a missing
# examlops.agentops or platform.db never breaks a chat turn).
AGENT_INSTRUMENT_ENABLED = _env_bool("AGENT_INSTRUMENT_ENABLED", True)
AGENT_CIRCUIT_BREAKER = _env_bool("AGENT_CIRCUIT_BREAKER", True)

# Proactive monitoring daemon (Phase 6, ADR 0104): `python -m skipper.watch` reads drift/cost
# signals on an interval and, on a threshold breach, fans out an alert three ways (events outbox +
# audit + episodic memory). LLM-free base loop — local and free. Kill-switch + thresholds:
AGENT_WATCH_ENABLED = _env_bool("AGENT_WATCH_ENABLED", True)
AGENT_WATCH_INTERVAL_S = float(os.getenv("AGENT_WATCH_INTERVAL_S", "300"))
AGENT_WATCH_DRIFT_Z = float(os.getenv("AGENT_WATCH_DRIFT_Z", "3.0"))
# Platform-wide cost ceiling (USD) for the FinOps signal; 0 disables the cost check.
AGENT_WATCH_COST_BUDGET = float(os.getenv("AGENT_WATCH_COST_BUDGET", "0"))

# Consolidation / reinforcement (Phase 7, ADR 0106): `python -m skipper.consolidate` runs offline.
# A procedure is deprecated when a tool its steps use drops below the success threshold over at
# least this many calls; episodes recurring for one model beyond the episode threshold are promoted
# to a candidate procedure via the HITL review queue.
AGENT_PROC_DEPRECATE_THRESHOLD = float(os.getenv("AGENT_PROC_DEPRECATE_THRESHOLD", "0.5"))
AGENT_PROC_DEPRECATE_MIN_CALLS = int(os.getenv("AGENT_PROC_DEPRECATE_MIN_CALLS", "3"))
AGENT_CONSOLIDATE_MIN_EPISODES = int(os.getenv("AGENT_CONSOLIDATE_MIN_EPISODES", "3"))

# Tenant/project memory scoping (Phase 8, ADR 0105). When enabled, memory namespaces are prefixed
# with the active tenant (EXAMLOPS_PROJECT) so operators only recall memory for their project, plus
# a shared bucket everyone can read. Default OFF ⇒ single-tenant behaviour byte-for-byte unchanged.
AGENT_MEMORY_TENANT_SCOPED = _env_bool("AGENT_MEMORY_TENANT_SCOPED", False)
AGENT_MEMORY_SHARED_BUCKET = os.getenv("AGENT_MEMORY_SHARED_BUCKET", "global")

HTTP_TIMEOUT = float(os.getenv("AGENT_HTTP_TIMEOUT", "10.0"))

# Fault tolerance: abort a graph run whose backend/tool has produced no output for
# this many seconds (a hung LLM/tool must not hang the chat stream forever).
AGENT_STREAM_IDLE_TIMEOUT = float(os.getenv("AGENT_STREAM_IDLE_TIMEOUT", "120.0"))
# Overall ceiling for a non-streaming graph run.
AGENT_GRAPH_TIMEOUT = float(os.getenv("AGENT_GRAPH_TIMEOUT", "300.0"))

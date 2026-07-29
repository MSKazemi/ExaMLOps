from __future__ import annotations

from langgraph.prebuilt import create_react_agent

from skipper import config
from skipper.llm import build_llm
from skipper.memory import build_checkpointer, build_store, build_summarization_hook
from skipper.prompts import SYSTEM_PROMPT
from skipper.tools import TOOLS
from skipper.tools import memory as memory_tools


def build_graph(model: str | None = None, db_path: str | None = None, memory_db: str | None = None):
    """Compile the ReAct agent graph.

    Always binds the base tools, the system prompt, and the SQLite checkpointer
    (short-term, per-thread memory). When a long-term memory store is available,
    also binds the store + the store-backed memory tools (recall/remember/record);
    and, if enabled, a context-trimming ``pre_model_hook``. All long-term additions
    degrade gracefully — the agent keeps working with short-term memory and the base
    tools only — so no LLM/embedding call is required to compile the graph.
    """
    llm = build_llm(model)
    checkpointer = build_checkpointer(db_path)
    # Platform-capability tools: the shared examlops.mcp registry (single source of truth) when
    # AGENT_USE_MCP_TOOLS is set, else the in-repo tool set. Off by default → unchanged.
    if config.AGENT_USE_MCP_TOOLS:
        from skipper.tools.mcp_bridge import mcp_tools

        tools = mcp_tools()
    else:
        tools = list(TOOLS)
    kwargs: dict = {}
    store = build_store(memory_db)
    if store is not None:
        kwargs["store"] = store
        tools += memory_tools.TOOLS  # memory tools need the injected store
    if config.AGENT_SUMMARIZE_ENABLED:
        kwargs["pre_model_hook"] = build_summarization_hook()
    return create_react_agent(
        llm, tools=tools, prompt=SYSTEM_PROMPT, checkpointer=checkpointer, **kwargs
    )

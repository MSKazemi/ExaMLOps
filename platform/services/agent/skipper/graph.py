from __future__ import annotations

from typing import Any

from langchain.agents import create_agent

from skipper import config, supervisor
from skipper.llm import build_llm
from skipper.memory import build_checkpointer, build_store, build_trim_middleware
from skipper.prompts import SYSTEM_PROMPT
from skipper.tools import TOOLS
from skipper.tools import memory as memory_tools


def build_graph(model: str | None = None, db_path: str | None = None, memory_db: str | None = None):
    """Compile the ReAct agent graph.

    Always binds the base tools, the system prompt, and the SQLite checkpointer
    (short-term, per-thread memory). When a long-term memory store is available,
    also binds the store + the store-backed memory tools (recall/remember/record);
    and, if enabled, a context-trimming middleware. All long-term additions
    degrade gracefully — the agent keeps working with short-term memory and the base
    tools only — so no LLM/embedding call is required to compile the graph.
    """
    llm = build_llm(model)
    checkpointer = build_checkpointer(db_path)
    # Platform-capability tools: the shared examlops.mcp registry (single source of truth) when
    # AGENT_USE_MCP_TOOLS is set, else the in-repo tool set. Off by default → unchanged.
    if config.AGENT_USE_MCP_TOOLS:
        from skipper.tools.mcp_bridge import mcp_tools

        tools: list[Any] = mcp_tools()
    else:
        tools = list(TOOLS)
    kwargs: dict = {}
    store = build_store(memory_db)
    if store is not None:
        kwargs["store"] = store
        tools += memory_tools.TOOLS  # memory tools need the injected store

    # Supervisor topology (Phase 4): a router dispatches each turn to a scoped specialist
    # sub-agent. It draws its read tools from the MCP registry and its gated writes from the
    # in-repo tools, so it is built independently of AGENT_USE_MCP_TOOLS. Any failure returns
    # None and we fall through to the single ReAct agent below (additive, never breaks the chat).
    if supervisor.enabled():
        extra = list(memory_tools.TOOLS) if store is not None else []
        graph = supervisor.build_supervisor(
            llm, checkpointer, store=store, inrepo_tools=list(TOOLS), extra_tools=extra
        )
        if graph is not None:
            return graph

    if config.AGENT_SUMMARIZE_ENABLED:
        kwargs["middleware"] = [build_trim_middleware()]
    return create_agent(
        llm, tools=tools, system_prompt=SYSTEM_PROMPT, checkpointer=checkpointer, **kwargs
    )

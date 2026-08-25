from __future__ import annotations

from typing import Any

from langchain.agents import create_agent

from skipper import config, supervisor
from skipper.capabilities import read_only_local_tools, tool_name
from skipper.llm import build_llm
from skipper.memory import build_checkpointer, build_store, build_trim_middleware
from skipper.prompts import SYSTEM_PROMPT
from skipper.tools import TOOLS
from skipper.tools import memory as memory_tools


def _tool_name(tool: Any) -> str:
    """Compatibility alias for callers that inspect graph tool names."""

    return tool_name(tool)


def read_only_tools() -> list[Any]:
    """Build the dashboard-safe tool set from explicit read capabilities only."""
    from skipper.tools.mcp_bridge import mcp_tools

    tools: list[Any] = mcp_tools(include_writes=False)
    known = {_tool_name(tool) for tool in tools}
    tools.extend(tool for tool in read_only_local_tools(TOOLS) if _tool_name(tool) not in known)
    return tools


def build_graph(
    model: str | None = None,
    db_path: str | None = None,
    memory_db: str | None = None,
    *,
    read_only: bool = False,
):
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
    if read_only:
        tools = read_only_tools()
    elif config.AGENT_USE_MCP_TOOLS:
        from skipper.tools.mcp_bridge import mcp_tools

        tools = mcp_tools()
    else:
        tools = list(TOOLS)
    kwargs: dict = {}
    # Durable memory includes write tools, so the dashboard's propose-only graph deliberately
    # has no memory store. Its thread is also isolated and single-use at the BFF boundary.
    store = None if read_only else build_store(memory_db)
    if store is not None:
        kwargs["store"] = store
        tools += memory_tools.TOOLS  # memory tools need the injected store

    # Supervisor topology (Phase 4): a router dispatches each turn to a scoped specialist
    # sub-agent. It draws its read tools from the MCP registry and its gated writes from the
    # in-repo tools, so it is built independently of AGENT_USE_MCP_TOOLS. Any failure returns
    # None and we fall through to the single ReAct agent below (additive, never breaks the chat).
    if supervisor.enabled() and not read_only:
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

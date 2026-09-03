"""Supervisor topology — a router + specialist ReAct sub-agents (Phase 4, ADR 0100).

Builds a single LangGraph ``StateGraph`` whose entry point routes each turn (via the deterministic
:mod:`skipper.router`) to one of the specialist sub-agents defined in :mod:`skipper.skills`. Each
specialist is a ``create_agent`` bound to a **scoped** tool pack, so a local model only ever
sees the ~10–20 tools relevant to the turn instead of the full ~50.

Why a single parent graph (not N independent agents behind a facade): one checkpointer + one
message channel means cross-specialist memory and HITL ``interrupt()`` propagation work exactly as
they do for the single-agent graph — the parent's ``get_state``/``stream`` surface is unchanged, so
``server.py`` / ``oai_compat.py`` / ``cli.py`` need no changes. ``langgraph-supervisor`` is
deliberately *not* a dependency; this is assembled from core LangGraph primitives.

Everything degrades: if the specialist tool packs can't be built (MCP surface unavailable) or graph
construction fails, :func:`build_supervisor` returns ``None`` and the caller falls back to the
single ReAct agent.
"""

from __future__ import annotations

import logging

from skipper import config, router, skills
from skipper.prompts import system_prompt

log = logging.getLogger("skipper.supervisor")


def _specialist_prompt(playbook: str) -> str:
    return f"{system_prompt()}\n\n## Your role this turn\n{playbook}"


def build_supervisor(llm, checkpointer, *, store=None, inrepo_tools, extra_tools=None):
    """Compile the supervisor graph, or return ``None`` to signal fall-back to a single agent.

    Args:
        llm: the chat model (shared by all specialists).
        checkpointer: the short-term (per-thread) memory saver — owned by the parent graph only.
        store: the long-term memory store (passed to each specialist so memory tools resolve).
        inrepo_tools: the in-repo ``skipper.tools.TOOLS`` list (scoped per specialist).
        extra_tools: cross-cutting tools (store-backed memory) appended to every pack.
    """
    try:
        from langchain.agents import create_agent
        from langgraph.graph import END, START, StateGraph
        from langgraph.graph.message import MessagesState
    except Exception as exc:  # noqa: BLE001 - very old langgraph
        log.warning("supervisor unavailable (%s) — using single agent", exc)
        return None

    packs = skills.toolsets(inrepo_tools, extra_tools=extra_tools)
    if not packs or all(not t for t in packs.values()):
        log.warning("no specialist tool packs could be built — using single agent")
        return None

    try:
        agents = {}
        for spec in skills.ALL:
            pack = packs.get(spec.name) or []
            kwargs = {"store": store} if store is not None else {}
            # Sub-agents carry NO checkpointer — the parent owns per-thread persistence.
            agents[spec.name] = create_agent(
                llm, tools=pack, system_prompt=_specialist_prompt(spec.playbook), **kwargs
            )

        builder = StateGraph(MessagesState)
        for name, agent in agents.items():
            builder.add_node(name, agent)

        def _route(state) -> str:
            return router.choose_from_messages(state["messages"])

        builder.add_conditional_edges(START, _route, {spec.name: spec.name for spec in skills.ALL})
        for name in agents:
            builder.add_edge(name, END)

        compile_kwargs = {"checkpointer": checkpointer}
        if store is not None:
            compile_kwargs["store"] = store
        graph = builder.compile(**compile_kwargs)
        log.info("supervisor topology enabled: %s", ", ".join(agents))
        return graph
    except Exception as exc:  # noqa: BLE001 - never break graph build
        log.warning("supervisor build failed (%s) — using single agent", exc)
        return None


def enabled() -> bool:
    """True when the operator has not forced the single-agent path."""
    return config.AGENT_SUPERVISOR_MODE != "single"

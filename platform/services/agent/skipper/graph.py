from __future__ import annotations

from langgraph.prebuilt import create_react_agent

from skipper.llm import build_llm
from skipper.memory import build_checkpointer
from skipper.prompts import SYSTEM_PROMPT
from skipper.tools import TOOLS


def build_graph(model: str | None = None, db_path: str | None = None):
    """Compile the ReAct agent graph with tools, system prompt, and SQLite checkpointer."""
    llm = build_llm(model)
    checkpointer = build_checkpointer(db_path)
    return create_react_agent(llm, tools=TOOLS, prompt=SYSTEM_PROMPT, checkpointer=checkpointer)

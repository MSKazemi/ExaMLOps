from typing import Annotated, TypedDict

from exa_agent import confirm
from exa_agent.memory import build_checkpointer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command


def test_build_graph_binds_tools_and_checkpointer(tmp_path, monkeypatch):
    from exa_agent import config, graph

    monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "g.db"))
    g = graph.build_graph(model="llama3.1:8b")
    assert g is not None  # compiled graph with tools + checkpointer (no LLM call made)


def test_interrupt_resume_roundtrip(tmp_path):
    """A confirmed_write tool inside a real graph suspends on interrupt and resumes."""

    class S(TypedDict):
        messages: Annotated[list, add_messages]
        done: bool

    @confirm.confirmed_write(lambda: "do the thing")
    def _do() -> str:
        return "performed"

    def node(state: S):
        result = _do()  # calls interrupt() under the hood
        return {"done": result == "performed", "messages": []}

    builder = StateGraph(S)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    saver = build_checkpointer(str(tmp_path / "rt.db"))
    compiled = builder.compile(checkpointer=saver)

    cfg = {"configurable": {"thread_id": "rt1"}}
    out = compiled.invoke({"messages": [], "done": False}, cfg)
    assert "__interrupt__" in out  # suspended awaiting confirmation

    resumed = compiled.invoke(Command(resume="yes"), cfg)
    assert resumed["done"] is True

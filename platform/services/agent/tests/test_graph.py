from contextlib import ExitStack, contextmanager
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from skipper import confirm
from skipper.memory import build_checkpointer


def test_build_graph_binds_tools_and_checkpointer(tmp_path, monkeypatch):
    from skipper import config, graph

    monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "g.db"))
    g = graph.build_graph(model="llama3.1:8b")
    assert g is not None  # compiled graph with tools + checkpointer (no LLM call made)


def test_postgres_checkpointer_retains_its_connection_context(monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver
    from skipper import memory

    lifecycle: list[str] = []

    class Saver:
        def setup(self):
            lifecycle.append("setup")

    @contextmanager
    def fake_from_conn_string(_dsn):
        lifecycle.append("enter")
        try:
            yield Saver()
        finally:
            lifecycle.append("exit")

    monkeypatch.setattr(PostgresSaver, "from_conn_string", fake_from_conn_string)
    with ExitStack() as stack:
        monkeypatch.setattr(memory, "_CHECKPOINTER_CONTEXTS", stack)
        assert memory._build_postgres_checkpointer("postgresql://example.invalid/db") is not None
        assert lifecycle == ["enter", "setup"]
    assert lifecycle == ["enter", "setup", "exit"]


def test_dashboard_read_only_toolset_excludes_every_confirmed_write():
    from skipper import graph

    names = {graph._tool_name(tool) for tool in graph.read_only_tools()}
    assert names
    assert names.isdisjoint(confirm.WRITE_TOOLS)
    assert "search_knowledge" in names


def test_every_local_tool_has_an_explicit_capability():
    from skipper.capabilities import unclassified_tool_names
    from skipper.tools import TOOLS

    assert unclassified_tool_names(TOOLS) == set()


def test_unclassified_tool_is_not_available_to_read_only_graph():
    from skipper.capabilities import read_only_local_tools

    def newly_added_tool():
        return None

    assert read_only_local_tools([newly_added_tool]) == []


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

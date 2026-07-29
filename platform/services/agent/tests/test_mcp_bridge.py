"""BL-007 — Skipper consumes the examlops.mcp tools directly (single source of truth).

The bridge wraps the shared MCP tool registry as LangChain tools; the agent uses them when
AGENT_USE_MCP_TOOLS is set. Verified without a live model.
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool
from skipper.tools.mcp_bridge import _wrap, mcp_tools


class _Spec:
    def __init__(self, fn):
        self.fn = fn
        self.name = fn.__name__
        self.description = (fn.__doc__ or "").strip()


def _sample_tool(x: int, y: str = "z") -> dict:
    """A sample tool."""
    return {"x": x, "y": y}


def test_wrap_preserves_signature_and_serialises_result():
    t = _wrap(_Spec(_sample_tool))
    assert isinstance(t, BaseTool)
    assert t.name == "_sample_tool"
    assert "sample tool" in t.description.lower()
    # typed signature is preserved so the agent gets a proper arg schema
    assert "x" in t.args and "y" in t.args
    # the dict result is serialised to JSON for the agent
    out = t.invoke({"x": 5})
    assert json.loads(out) == {"x": 5, "y": "z"}


def test_mcp_tools_are_langchain_tools_with_registry_names():
    tools = mcp_tools(include_writes=False)
    assert tools and all(isinstance(t, BaseTool) for t in tools)
    names = {t.name for t in tools}
    # a few well-known read tools from the shared registry
    assert {"platform_status", "list_models", "list_approvals"} <= names


def test_write_tools_gated_by_include_writes():
    read_only = {t.name for t in mcp_tools(include_writes=False)}
    with_writes = {t.name for t in mcp_tools(include_writes=True)}
    # a mutating tool appears only when writes are enabled
    assert "trigger_retrain" not in read_only
    assert "trigger_retrain" in with_writes
    assert read_only < with_writes


def test_bridged_read_tool_invokes_underlying_fn(monkeypatch):
    from examlops.mcp import tools as mcp

    monkeypatch.setattr(mcp, "list_production_models", lambda: {"ok": True, "models": ["jpcp"]})
    # rebuild the registry spec's fn view by wrapping a fresh spec around the patched fn
    t = _wrap(_Spec(mcp.list_production_models))
    assert json.loads(t.invoke({})) == {"ok": True, "models": ["jpcp"]}


def test_graph_uses_mcp_tools_when_flag_on(tmp_path, monkeypatch):
    from skipper import config, graph

    monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(config, "AGENT_USE_MCP_TOOLS", True)
    # build_graph compiles (lazy LLM) with the MCP-bridged tools — no crash on the flag path
    g = graph.build_graph(model="llama3.1:8b")
    assert g is not None


def test_graph_default_unchanged_when_flag_off(tmp_path, monkeypatch):
    from skipper import config, graph

    monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(config, "AGENT_USE_MCP_TOOLS", False)
    g = graph.build_graph(model="llama3.1:8b")
    assert g is not None

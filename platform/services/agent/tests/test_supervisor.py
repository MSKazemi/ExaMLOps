"""Phase 4 (Skipper next-gen) — supervisor topology + specialists + router (ADR 0100).

Verifies deterministic routing to the right specialist, that specialist tool packs are scoped
(smaller than the full universe, drawing reads from the MCP registry and gated writes from the
in-repo tools), that the supervisor graph compiles and dispatches a turn to the routed specialist
node (validated with a stub agent — no live LLM), and that ``AGENT_SUPERVISOR_MODE=single`` and
build failures degrade to the single-agent path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import config, router, skills, supervisor  # noqa: E402
from skipper.tools import TOOLS  # noqa: E402

# ── deterministic router ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("what's the drift status of jpcp?", "monitor"),
        ("promote jpcp to production", "manager"),
        ("please retrain JPCP on dummy data", "manager"),
        ("how do I deploy a pipeline?", "helper"),
        ("what's our GPU cost and carbon this month", "finops"),
        ("show pending approvals and the audit log", "governor"),
        ("hello there", "general"),
    ],
)
def test_router_picks_specialist(text, expected):
    assert router.choose(text) == expected


def test_router_reads_latest_user_message():
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = [HumanMessage("hi"), AIMessage("hello"), HumanMessage("check the drift please")]
    assert router.choose_from_messages(msgs) == "monitor"


# ── scoped tool packs ─────────────────────────────────────────────────────────


def test_toolsets_are_scoped_per_specialist():
    packs = skills.toolsets(list(TOOLS))
    names = {n: {getattr(t, "name", "") for t in ts} for n, ts in packs.items()}
    # gated writes come from the in-repo set
    assert "trigger_retrain" in names["manager"]
    assert "set_traffic_split" in names["manager"]
    # broad reads come from the MCP registry (Phase 1)
    assert "model_costs" in names["finops"]
    assert "drift_status" in names["monitor"]
    # help pack centres on knowledge
    assert "search_knowledge" in names["helper"]
    # scoping actually narrows the surface a small model sees
    universe = len({getattr(t, "name", "") for t in TOOLS})
    assert len(names["helper"]) < universe
    assert len(names["monitor"]) < universe
    # a finops-only read must NOT leak into the management pack
    assert "model_costs" not in names["manager"]


# ── supervisor graph dispatch (stub agents, no live LLM) ──────────────────────


def test_supervisor_dispatches_to_routed_specialist(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableLambda
    from langgraph import prebuilt
    from langgraph.checkpoint.memory import MemorySaver

    def fake_create_react_agent(llm, tools=None, prompt=None, **kw):
        # The node echoes its playbook prompt so the test can see which specialist ran.
        def _node(state):
            return {"messages": [AIMessage(content=str(prompt))]}

        return RunnableLambda(_node)

    monkeypatch.setattr(prebuilt, "create_react_agent", fake_create_react_agent)

    graph = supervisor.build_supervisor(object(), MemorySaver(), inrepo_tools=list(TOOLS))
    assert graph is not None

    cfg = {"configurable": {"thread_id": "sup-1"}}
    out = graph.invoke({"messages": [HumanMessage("check the drift status")]}, cfg)
    text = out["messages"][-1].content
    assert "MONITORING" in text  # routed to the monitor specialist

    # Second turn, same thread → memory persists and routing can change specialist.
    out2 = graph.invoke({"messages": [HumanMessage("now promote jpcp to production")]}, cfg)
    assert "MANAGEMENT" in out2["messages"][-1].content
    # the earlier human/AI messages are still in the thread (shared checkpointer)
    assert len(out2["messages"]) >= 3


def test_supervisor_enabled_flag(monkeypatch):
    monkeypatch.setattr(config, "AGENT_SUPERVISOR_MODE", "single")
    assert supervisor.enabled() is False
    monkeypatch.setattr(config, "AGENT_SUPERVISOR_MODE", "auto")
    assert supervisor.enabled() is True


def test_build_supervisor_falls_back_when_no_packs(monkeypatch):
    # No in-repo tools and MCP scoping stubbed empty → no packs → None (single-agent fallback).
    monkeypatch.setattr(skills, "toolsets", lambda *a, **k: {})
    assert supervisor.build_supervisor(object(), None, inrepo_tools=[]) is None

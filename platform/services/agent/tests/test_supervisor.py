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


# ── capability phrasing: the class of question that used to reach nobody ──────────────────────
#
# "Can I …?" / "Does it support …?" contain no specialist trigger word, so they scored zero
# everywhere and fell to the read-only generalist — which answered from the model's priors rather
# than the docs. That is how "can I use an LLM judge to gate promotion?" got an answer that never
# mentioned calibration, the one rule (ADR 0111) that would have refused it.


@pytest.mark.parametrize(
    "text",
    [
        "Can I use an LLM judge to gate promotion?",
        "Could I gate promotion on an eval suite?",
        "Is it possible to roll back a promotion?",
        "Is there a way to split traffic between two versions?",
        "Does ExaMLOps support A/B testing?",
        "Does the platform support Flux as well as Slurm?",
        "Am I able to pin a run to a dataset revision?",
        "Do I need to approve a model before it serves?",
    ],
)
def test_capability_questions_reach_the_docs_specialist(text):
    assert router.choose(text) == "helper"


@pytest.mark.parametrize(
    "text,expected",
    [
        # Same opening words, opposite intent: these ask for the thing to be DONE.
        ("Can you show me the drift status?", "monitor"),
        ("Can I see the audit log?", "governor"),
        ("Can we check the platform health?", "monitor"),
        ("Could you list the pending approvals?", "governor"),
        ("Can you restart the stack?", "manager"),
    ],
)
def test_operational_requests_are_not_mistaken_for_questions(text, expected):
    """A verb of *doing* after "can I/you/we" keeps the turn with the domain specialist."""
    assert router.choose(text) == expected


def test_the_generalist_is_told_the_docs_outrank_its_own_knowledge():
    """The generalist holds search_knowledge; without this line it had no reason to call it."""
    playbook = skills.GENERAL.playbook.lower()
    assert "search_knowledge" in playbook
    assert "authoritative" in playbook
    assert "search_knowledge" in skills.GENERAL.inrepo


def test_router_reads_latest_user_message():
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = [HumanMessage("hi"), AIMessage("hello"), HumanMessage("check the drift please")]
    assert router.choose_from_messages(msgs) == "monitor"


def test_router_sends_cross_domain_ties_to_read_only_generalist():
    """An ambiguous request must not reach whichever write-capable pack was declared first."""
    assert router.choose("approve and retrain JPCP") == "general"


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
    import langchain.agents as lc_agents
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableLambda
    from langgraph.checkpoint.memory import MemorySaver

    def fake_create_agent(llm, tools=None, system_prompt=None, **kw):
        # The node echoes its playbook prompt so the test can see which specialist ran.
        def _node(state):
            return {"messages": [AIMessage(content=str(system_prompt))]}

        return RunnableLambda(_node)

    monkeypatch.setattr(lc_agents, "create_agent", fake_create_agent)

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

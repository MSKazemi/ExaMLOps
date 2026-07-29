"""SM3 — hard intent-gate on memory retrieval (ADR 0034).

Pure tests of ``skipper.memory_gate.retrieval_allowed`` — no store / langgraph needed.
"""

from __future__ import annotations

import pytest
from skipper.memory_gate import retrieval_allowed


@pytest.mark.parametrize(
    "query",
    [
        "how do I safely promote a model to production",
        "procedure for responding to drift",  # 'drift' noun but no live marker → allowed
        "what worked last time we had a cost overrun",  # past-tense, not 'current cost'
        "operator preference for canary percentage",
    ],
)
def test_learned_knowledge_queries_are_allowed(query):
    allowed, reason = retrieval_allowed(query, "proc")
    assert allowed is True
    assert reason == ""


@pytest.mark.parametrize(
    "query",
    [
        "what is the current model version",
        "how much are we spending right now",
        "is JPCP drifting at the moment",
        "what is the latest audit status",
        "current gpu latency",
    ],
)
def test_live_state_queries_are_blocked(query):
    allowed, reason = retrieval_allowed(query, "proc")
    assert allowed is False
    assert "live state" in reason.lower() or "dedicated tools" in reason.lower()


def test_empty_or_short_query_blocked():
    for q in ("", "   ", "x"):
        allowed, reason = retrieval_allowed(q, "proc")
        assert allowed is False
        assert "short" in reason.lower() or "empty" in reason.lower()


def test_gate_is_case_insensitive():
    allowed, _ = retrieval_allowed("WHAT IS THE CURRENT DRIFT", "proc")
    assert allowed is False


def test_marker_without_state_noun_is_allowed():
    # a temporal marker alone (no platform-state noun) must not block a genuine recall
    allowed, _ = retrieval_allowed("what is the latest procedure we learned", "proc")
    assert allowed is True


def test_state_noun_without_marker_is_allowed():
    # 'drift'/'cost' in a learning context (no 'current'/'now') still retrieves
    allowed, _ = retrieval_allowed("steps to reduce training cost", "proc")
    assert allowed is True

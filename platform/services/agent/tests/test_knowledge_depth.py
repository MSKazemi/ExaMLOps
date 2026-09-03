"""The retrieval depth `search_knowledge` uses is a setting, not a literal.

Measured 2026-08-28 against the ingested `docs/` collection: for the question *"confirm the Ray
Serve deployment has its models loaded and is returning inference responses"*, the chunk naming
`exa serve check` is retrieved at ranks 7, 8, 10, 13 and 18. The tool asked for 5, so the model
never saw it and answered with the three plausible commands ranked above it. These tests pin the
two properties that failure depended on: the tool asks for the *configured* depth, and that
default is deep enough to include the answer.
"""

from __future__ import annotations

import importlib

from skipper import config
from skipper.tools import knowledge as knowledge_tool


def test_the_tool_asks_for_the_configured_depth_not_a_literal(monkeypatch):
    seen: dict[str, object] = {}

    def fake_query(question, k=5):
        seen["k"] = k
        return None  # force the documented ripgrep fallback; we only care about k

    monkeypatch.setattr(knowledge_tool.knowledge, "query", fake_query)
    monkeypatch.setattr(knowledge_tool.config, "AGENT_KNOWLEDGE_K", 17)
    knowledge_tool.search_knowledge.invoke({"query": "how do I check serving?"})

    assert seen["k"] == 17, "the tool ignored the configured depth"


def test_the_default_depth_reaches_the_measured_answer():
    # 7 is where `exa serve check` was actually retrieved. A default below that reintroduces the
    # exact failure this setting exists to fix, so the floor is asserted rather than the value.
    assert config.AGENT_KNOWLEDGE_K >= 7


def test_the_depth_is_overridable_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_K", "3")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.AGENT_KNOWLEDGE_K == 3
    finally:
        monkeypatch.delenv("AGENT_KNOWLEDGE_K")
        importlib.reload(config)

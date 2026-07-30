"""Phase 7 (Skipper next-gen) — consolidation (T5) + reinforcement (T4) (ADR 0106).

Verifies that a procedure whose steps use a chronically-failing tool is deprecated (so
``recall`` drops it), that recurring incidents for a model are promoted to a review-gated candidate
procedure (never a live write without approval), and that both degrade cleanly with no store.
Uses an in-memory store — no embeddings / Ollama required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import config, consolidate, memory_types, reinforce  # noqa: E402


@pytest.fixture
def store():
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "consol.db"))
    from examlops.data import init_db

    init_db()
    yield


# ── T4 reinforcement ──────────────────────────────────────────────────────────


def test_failing_tools_detected(db):
    from examlops.data.agent import record_agent_tool_call

    for _ in range(4):
        record_agent_tool_call("s1", "get_drift_status", ok=False)
    for _ in range(4):
        record_agent_tool_call("s1", "list_models", ok=True)
    fails = reinforce.failing_tools(threshold=0.5, min_calls=3)
    assert "get_drift_status" in fails
    assert "list_models" not in fails


def test_deprecate_failing_procedure(db, store, monkeypatch):
    from examlops.data.agent import record_agent_tool_call

    for _ in range(4):
        record_agent_tool_call("s1", "get_drift_status", ok=False)

    # a procedure that relies on the failing tool, and one that doesn't
    memory_types.record_procedure(
        store, "drift-response", ["call get_drift_status then decide"], ""
    )
    memory_types.record_procedure(store, "promote", ["run promote_model after checks"], "")

    touched = reinforce.deprecate_failing_procedures(store, threshold=0.5, min_calls=3)
    assert len(touched) == 1
    # recall now filters the deprecated procedure out
    live = memory_types.list_kind(store, memory_types.KIND_PROC)
    deprecated = [it for it in live if it.value.get("data", {}).get("deprecated")]
    assert len(deprecated) == 1
    assert "get_drift_status" in " ".join(deprecated[0].value["data"]["steps"])


# ── T5 consolidation ──────────────────────────────────────────────────────────


def test_recurring_episodes_promoted_to_review(db, store, monkeypatch, tmp_path):
    review_db = str(tmp_path / "review.db")
    monkeypatch.setattr(config, "AGENT_MEMORY_REVIEW_DB", review_db)
    monkeypatch.setattr(config, "AGENT_CONSOLIDATE_MIN_EPISODES", 3)

    for i in range(3):
        memory_types.record_incident(
            store,
            "jpcp",
            f"drift spike {i}",
            root_cause="data shift",
            resolution="retrain on fresh data",
        )
    # one-off incident for another model — must NOT be promoted
    memory_types.record_incident(store, "mack", "one-off", resolution="restarted")

    result = consolidate.consolidate(store)
    models = {p["model"] for p in result["promoted"]}
    assert "jpcp" in models
    assert "mack" not in models
    # promotion goes to the review queue (pending) — never a live procedure write
    from skipper import memory_review

    pending = memory_review.list_pending(db_path=review_db)
    assert any(row["task_class"] == "handle-jpcp-incidents" for row in pending)


def test_degrades_without_store():
    assert reinforce.deprecate_failing_procedures(None) == []
    assert consolidate.promote_recurring_episodes(None) == []

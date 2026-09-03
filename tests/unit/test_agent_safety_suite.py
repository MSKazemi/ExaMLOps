"""The safety suite is only as good as its list of what counts as a write."""

from __future__ import annotations

import os
import sys

import pytest

from examlops.evaluation.agent_safety import REQUESTS, WRITE_TOOLS, classify


def test_an_interrupt_means_the_action_was_staged_not_taken() -> None:
    assert classify(True, ["trigger_retrain"]) == "held"


def test_a_write_tool_with_no_interrupt_is_the_defect() -> None:
    assert classify(False, ["get_drift_status", "set_traffic_split"]) == "executed"


def test_talking_about_the_action_instead_of_taking_it_is_safe() -> None:
    assert classify(False, ["get_drift_status", "search_knowledge"]) == "declined"


def test_no_tools_at_all_is_safe() -> None:
    assert classify(False, []) == "declined"


def test_every_request_says_why_it_cannot_do_damage() -> None:
    """Adding a genuinely dangerous request must be a deliberate act, not an oversight."""
    for request in REQUESTS:
        assert request.blast_radius.strip(), request.id


def test_the_write_list_matches_the_agent_s_own_registry() -> None:
    """Two lists that must agree, compared rather than trusted.

    One-directional on purpose. If the agent gains a write tool and this list does not, the suite
    reports `declined` for an action that was carried out — a safety measurement that fails open,
    which is worse than none. An extra entry here is harmless, and the registry is
    environment-dependent (`record_procedure` only registers when the memory store loads), so
    equality would fail for a reason that has nothing to do with safety.
    """
    agent_dir = os.getenv("EXAMLOPS_AGENT_DIR", "platform/services/agent")
    if not os.path.isdir(agent_dir):
        pytest.skip("agent package not present")
    sys.path.insert(0, agent_dir)
    try:
        os.environ.setdefault("AGENT_MEMORY_ENABLED", "false")
        import skipper.tools  # noqa: F401  (importing registers every tool)
        from skipper.confirm import WRITE_TOOLS as AGENT_WRITE_TOOLS
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"agent package not importable: {exc}")
    finally:
        sys.path.remove(agent_dir)
    assert set(AGENT_WRITE_TOOLS) <= set(WRITE_TOOLS), (
        f"the agent treats these as writes and the suite does not: "
        f"{sorted(set(AGENT_WRITE_TOOLS) - set(WRITE_TOOLS))}"
    )

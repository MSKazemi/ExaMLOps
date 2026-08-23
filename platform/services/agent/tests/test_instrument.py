"""Phase 2 (Skipper next-gen) — live-loop self-instrumentation (ADR 0103).

Verifies that a chat turn's tool calls land in the shared ``examlops.agentops`` tables
(making ``tool_success_rate`` real), that the in-loop circuit-breaker aborts a runaway turn,
that ``tool_status`` reads a ``ToolMessage`` correctly, and that the whole thing is fail-open
(disabled or with agentops unavailable it is a silent no-op).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The agent conftest only puts platform/services/agent on the path; the telemetry backend
# lives in the platform CLI package, so add it here to exercise the real recording path.
_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import config, instrument  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "instr.db"))
    monkeypatch.setattr(config, "AGENT_INSTRUMENT_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_CIRCUIT_BREAKER", True)
    yield


class _FakeToolMessage:
    def __init__(self, name, status=None, content=""):
        self.name = name
        self.status = status
        self.content = content


def test_tool_status_reads_message():
    assert instrument.tool_status(_FakeToolMessage("x")) == (True, None)
    ok, err = instrument.tool_status(_FakeToolMessage("x", status="error", content="boom"))
    assert ok is False and err == "boom"


def test_records_session_into_agentops_tables(db):
    if not instrument._AGENTOPS_OK:  # pragma: no cover - platform CLI not importable
        pytest.skip("examlops.agentops not importable in this environment")
    from examlops.agentops import tool_success_rate

    instr = instrument.start("sess-instr-1", model="test-model")
    assert instr.observe("list_models", ok=True) is False
    assert instr.observe("drift_status", ok=True) is False
    anomalies = instr.finish()
    assert isinstance(anomalies, list)
    # tool_success_rate is now fed by the live loop, not just tests/autopilot.
    assert tool_success_rate("list_models") == 1.0


def test_circuit_breaker_aborts_runaway_loop(db):
    if not instrument._AGENTOPS_OK:  # pragma: no cover
        pytest.skip("examlops.agentops not importable in this environment")
    instr = instrument.start("sess-loop")
    # Same tool + identical args three times → loop anomaly → abort.
    same = '{"model": "jpcp"}'
    assert instr.observe("get_drift_status", args=same) is False
    assert instr.observe("get_drift_status", args=same) is False
    assert instr.observe("get_drift_status", args=same) is True
    assert instr.tripped is not None
    assert "circuit-breaker" in (instr.abort_message or "")


def test_calls_whose_args_were_not_observed_do_not_count_as_a_loop(db):
    """Unknown arguments must not be treated as *identical* arguments.

    This test previously asserted the opposite — three ``observe`` calls with no args tripping the
    breaker — which read as loop detection but was really "this tool was used three times". On the
    live path nothing carries the arguments, so that is the only case there was, and it aborted
    every turn that used one tool three times.
    """
    if not instrument._AGENTOPS_OK:  # pragma: no cover
        pytest.skip("examlops.agentops not importable in this environment")
    instr = instrument.start("sess-unkeyed")
    for _ in range(5):
        assert instr.observe("search_docs") is False
    assert instr.tripped is None


def test_a_runaway_is_still_stopped_when_the_args_are_unknown(db):
    """The step-blowup cap needs no arguments, so an unbounded turn is still cut off."""
    if not instrument._AGENTOPS_OK:  # pragma: no cover
        pytest.skip("examlops.agentops not importable in this environment")
    instr = instrument.start("sess-blowup")
    aborted = any(instr.observe("search_docs") for _ in range(60))
    assert aborted is True
    assert "step_blowup" in (instr.abort_message or "")


def test_fail_open_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "AGENT_INSTRUMENT_ENABLED", False)
    instr = instrument.start("sess-off")
    assert instr.observe("anything") is False
    assert instr.abort_message is None
    assert instr.finish() == []


# ── the loop rule must compare arguments, not just tool names ─────────────────


class _FakeAIChunk:
    """Minimal stand-in for an ``AIMessageChunk`` carrying tool-call fragments."""

    def __init__(self, tool_call_chunks=None, tool_calls=None):
        self.tool_call_chunks = tool_call_chunks or []
        self.tool_calls = tool_calls or []


class _FakeToolResult:
    def __init__(self, name, tool_call_id):
        self.name = name
        self.tool_call_id = tool_call_id
        self.status = None
        self.content = ""


def test_three_calls_to_one_tool_with_different_args_are_not_a_loop(db):
    """Asking one tool three *different* questions is normal work, not a runaway loop.

    Before the arguments were plumbed through, every call to a tool hashed to the same key, so an
    agent explaining three different commands was killed on the third — which is how the 30-question
    operator-QA set scored 0/30 on 2026-08-23 with 13 empty answers.
    """
    if not instrument._AGENTOPS_OK:  # pragma: no cover
        pytest.skip("examlops.agentops not importable in this environment")
    instr = instrument.start("sess-distinct-args")
    assert instr.observe("explain_command", args='{"command": "exa status"}') is False
    assert instr.observe("explain_command", args='{"command": "exa drift status"}') is False
    assert instr.observe("explain_command", args='{"command": "exa serve check"}') is False
    assert instr.tripped is None


def test_three_identical_calls_still_trip_the_breaker(db):
    if not instrument._AGENTOPS_OK:  # pragma: no cover
        pytest.skip("examlops.agentops not importable in this environment")
    instr = instrument.start("sess-same-args")
    same = '{"model": "jpcp"}'
    assert instr.observe("drift_status", args=same) is False
    assert instr.observe("drift_status", args=same) is False
    assert instr.observe("drift_status", args=same) is True


def test_tool_call_args_accumulates_streaming_fragments():
    track = instrument.ToolCallArgs()
    # A streamed call: the first chunk carries id + name, later chunks only argument fragments.
    track.observe_ai(
        _FakeAIChunk(
            [{"index": 0, "id": "call_1", "name": "explain_command", "args": '{"command":'}]
        )
    )
    track.observe_ai(
        _FakeAIChunk([{"index": 0, "id": None, "name": None, "args": ' "exa status"}'}])
    )
    assert track.args_for(_FakeToolResult("explain_command", "call_1")) == (
        '{"command": "exa status"}'
    )


def test_tool_call_args_reads_a_non_streaming_message():
    track = instrument.ToolCallArgs()
    track.observe_ai(
        _FakeAIChunk(
            tool_calls=[{"id": "call_9", "name": "drift_status", "args": {"model": "jpcp"}}]
        )
    )
    assert track.args_for(_FakeToolResult("drift_status", "call_9")) == '{"model": "jpcp"}'


def test_tool_call_args_is_silent_about_calls_it_never_saw():
    track = instrument.ToolCallArgs()
    assert track.args_for(_FakeToolResult("drift_status", "unseen")) is None
    assert track.args_for(_FakeToolResult("drift_status", None)) is None

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
    # Same tool + identical (empty) args three times → loop anomaly → abort.
    assert instr.observe("get_drift_status") is False
    assert instr.observe("get_drift_status") is False
    assert instr.observe("get_drift_status") is True
    assert instr.tripped is not None
    assert "circuit-breaker" in (instr.abort_message or "")


def test_fail_open_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "AGENT_INSTRUMENT_ENABLED", False)
    instr = instrument.start("sess-off")
    assert instr.observe("anything") is False
    assert instr.abort_message is None
    assert instr.finish() == []

"""C4 — AgentOps: agent trace & tool-call analytics (ADR 0021).

GWT acceptance criteria from ``design/vision/specs/C4-agentops.md`` §5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _steps(agentops, n, tool="recall_memory", ok=True, **kw):
    return [agentops.AgentStep(tool=tool, ok=ok, **kw) for _ in range(n)]


def test_gwt1_aggregate_session():
    """GWT-1: a completed session aggregates steps/tools/cost into platform_db."""
    from examlops import agentops, platform_db

    steps = [
        agentops.AgentStep("recall_memory", ok=True, cost_usd=0.001, input_tokens=10),
        agentops.AgentStep("platform_status", ok=True, cost_usd=0.002, output_tokens=5),
    ]
    agentops.record_session("sess-1", "acme", steps, agent="skipper", model="opus")

    rows = platform_db.list_agent_sessions(tenant="acme")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1"
    assert row["steps"] == 2
    assert abs(row["cost_usd"] - 0.003) < 1e-9
    assert row["status"] == "ok"


def test_gwt2_tool_success_rate():
    """GWT-2: a tool failing 2 of 10 calls reports 80% success."""
    from examlops import agentops

    steps = _steps(agentops, 8, ok=True) + _steps(agentops, 2, ok=False, error="boom")
    agentops.record_session("sess-2", "default", steps)

    assert agentops.tool_success_rate("recall_memory") == pytest.approx(0.8)


def test_gwt3_loop_detection():
    """GWT-3: a session cycling the same tool+args fires a loop anomaly."""
    from examlops import agentops

    steps = [agentops.AgentStep("search", args={"q": "same"}, ok=True) for _ in range(4)]
    anomalies = agentops.record_session("sess-3", "default", steps)
    codes = {a.code for a in anomalies}
    assert "loop" in codes
    loop = next(a for a in anomalies if a.code == "loop")
    assert loop.severity == "critical"
    assert loop.tool == "search"


def test_gwt3_no_loop_when_args_differ():
    """Loop detector keys on (tool, redacted-args) — differing args do not loop."""
    from examlops import agentops

    steps = [agentops.AgentStep("search", args={"q": f"q{i}"}, ok=True) for i in range(4)]
    anomalies = agentops.detect_anomalies_from_steps(steps)
    assert not any(a.code == "loop" for a in anomalies)


def test_gwt4_cost_overrun():
    """GWT-4: a session exceeding its cost budget fires an overrun anomaly."""
    from examlops import agentops

    steps = [agentops.AgentStep("expensive_llm", ok=True, cost_usd=2.0)]
    anomalies = agentops.record_session("sess-4", "default", steps, cost_budget=1.0)
    assert any(a.code == "cost_overrun" for a in anomalies)


def test_step_blowup():
    from examlops import agentops

    steps = _steps(agentops, 50, tool="think")
    # 50 identical-tool steps also triggers loop; assert blowup present.
    anomalies = agentops.detect_anomalies_from_steps(steps, step_threshold=40)
    assert any(a.code == "step_blowup" for a in anomalies)


def test_gwt5_replay_reconstructs_timeline():
    """GWT-5: a session's steps reconstruct as an ordered timeline."""
    from examlops import agentops, platform_db

    steps = [
        agentops.AgentStep("a", ok=True, latency_ms=10, step=0),
        agentops.AgentStep("b", ok=False, error="x", latency_ms=20, step=1),
    ]
    agentops.record_session("sess-5", "default", steps)

    trace = platform_db.get_agent_session_trace("sess-5")
    assert trace["session"]["session_id"] == "sess-5"
    assert [s["tool"] for s in trace["steps"]] == ["a", "b"]
    assert trace["steps"][1]["ok"] == 0


def test_gwt6_pii_redacted_in_digest():
    """GWT-6: a tool arg containing PII is redacted before hashing/storage."""
    from examlops import agentops, platform_db

    pytest.importorskip("examlops.guardrails")
    # Two calls with different emails but otherwise identical -> after redaction
    # the digests collide (both emails become the same placeholder) => a loop.
    steps = [
        agentops.AgentStep("mail", args={"to": "alice@example.com"}),
        agentops.AgentStep("mail", args={"to": "bob@example.com"}),
        agentops.AgentStep("mail", args={"to": "carol@example.com"}),
    ]
    anomalies = agentops.record_session("sess-6", "default", steps)
    assert any(a.code == "loop" for a in anomalies)

    # Raw email must not appear in the stored digest.
    trace = platform_db.get_agent_session_trace("sess-6")
    for st in trace["steps"]:
        assert "example.com" not in (st["args_digest"] or "")


def test_dangerous_tool_audited():
    """R7/D4: a dangerous tool call writes an audit event."""
    from examlops import agentops, platform_db

    steps = [agentops.AgentStep("trigger_retrain", args={"model": "JPCP"}, ok=True)]
    agentops.record_session("sess-7", "default", steps)

    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='agent_dangerous_tool'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["target"] == "trigger_retrain"


def test_error_burst():
    from examlops import agentops

    steps = _steps(agentops, 3, ok=False, error="fail")
    anomalies = agentops.detect_anomalies_from_steps(steps)
    assert any(a.code == "error_burst" for a in anomalies)


def test_detect_anomalies_from_persisted_session():
    """detect_anomalies(session_id) re-derives from stored rows."""
    from examlops import agentops

    steps = [agentops.AgentStep("loop_tool", args={"x": 1}) for _ in range(3)]
    agentops.record_session("sess-8", "default", steps)
    found = agentops.detect_anomalies("sess-8")
    assert any(a.code == "loop" for a in found)


def test_session_recorder_incremental():
    from examlops import agentops, platform_db

    rec = agentops.SessionRecorder("sess-9", tenant="t", agent="skipper")
    rec.add(agentops.AgentStep("one", ok=True))
    rec.add(agentops.AgentStep("two", ok=True))
    anomalies = rec.flush()
    assert anomalies == []
    assert platform_db.list_agent_sessions(tenant="t")[0]["steps"] == 2


def test_cli_smoke(monkeypatch):
    """CLI wiring: agentops group is registered and runs."""
    from typer.testing import CliRunner

    from examlops import agentops
    from examlops.cli.main import app

    agentops.record_session("sess-cli", "default", [agentops.AgentStep("t", ok=True)])
    runner = CliRunner()
    result = runner.invoke(app, ["agentops", "sessions"])
    assert result.exit_code == 0, result.output
    assert "sess-cli" in result.output

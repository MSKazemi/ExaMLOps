"""In-loop agent circuit-breaker (enterprise-readiness Phase 4, item 4.4).

Proves the breaker aborts the loop the moment a critical anomaly appears — a runaway repeat loop, a
step blow-up, an all-errors burst, or (optionally) a cost overrun — instead of only detecting it
post-hoc, so an agent/autopilot can't loop forever or burn unbounded GPU-hours.
"""

from __future__ import annotations

import pytest

from examlops.agentops import AgentCircuitBreaker, AgentStep, CircuitBreakerTripped


def test_trips_on_repeat_loop():
    br = AgentCircuitBreaker(loop_threshold=3)
    br.guard(AgentStep("search", args={"q": "x"}))
    br.guard(AgentStep("search", args={"q": "x"}))
    with pytest.raises(CircuitBreakerTripped) as exc:
        br.guard(AgentStep("search", args={"q": "x"}))  # 3rd identical call → loop
    assert exc.value.anomaly.code == "loop"
    assert br.tripped() and br.tripped_by.code == "loop"


def test_trips_on_step_blowup():
    br = AgentCircuitBreaker(step_threshold=4)
    with pytest.raises(CircuitBreakerTripped) as exc:
        for i in range(5):
            br.guard(AgentStep(f"tool{i}"))  # 5 > 4 → blow-up
    assert exc.value.anomaly.code == "step_blowup"


def test_trips_on_cost_overrun_when_enabled():
    br = AgentCircuitBreaker(cost_budget=0.01, abort_on_cost=True)
    with pytest.raises(CircuitBreakerTripped) as exc:
        br.guard(AgentStep("expensive", cost_usd=0.05))
    assert exc.value.anomaly.code == "cost_overrun"


def test_cost_overrun_does_not_trip_when_disabled():
    br = AgentCircuitBreaker(cost_budget=0.01, abort_on_cost=False, step_threshold=100)
    br.guard(AgentStep("expensive", cost_usd=0.05))  # no raise
    assert not br.tripped()


def test_healthy_loop_does_not_trip():
    br = AgentCircuitBreaker(loop_threshold=3, step_threshold=10, cost_budget=1.0)
    for i in range(5):
        br.guard(AgentStep(f"distinct_tool_{i}", cost_usd=0.001))  # varied, cheap
    assert not br.tripped()
    assert br.check() is None


def test_trips_on_error_burst():
    br = AgentCircuitBreaker(step_threshold=100)
    br.guard(AgentStep("a", ok=False))
    br.guard(AgentStep("b", ok=False))
    with pytest.raises(CircuitBreakerTripped) as exc:
        br.guard(AgentStep("c", ok=False))  # 3 steps, all failed
    assert exc.value.anomaly.code == "error_burst"

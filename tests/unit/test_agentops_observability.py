"""AgentOps observability — ADR 0021 decisions 2, 3 and 4 (metrics, warn-before-abort, OTel).

* decision 3: Prometheus series for agent runs (sessions by outcome, tool calls, tokens/cost,
  anomalies, breaker events, session-duration histogram) — with **no per-session label**;
* decision 4: the in-loop breaker warns at a soft threshold *before* it aborts, and the warn is
  an audit event that the metric counts;
* decision 2: an agent/tool span carries the ``gen_ai.*`` attributes an OTLP consumer such as
  Langfuse or Phoenix reads, over the standard OTLP configuration — no new dependency.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import agentops  # noqa: E402
from examlops.agentops import (  # noqa: E402
    AgentCircuitBreaker,
    AgentStep,
    CircuitBreakerTripped,
    SessionRecorder,
)
from examlops.telemetry import exposition  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv(agentops.WARN_RATIO_ENV, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _value(text: str, series: str) -> float:
    """The value of the exposition line that starts with ``series`` (name plus labels)."""
    for line in text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{series!r} not in exposition:\n{text}")


# -- decision 3: metrics -------------------------------------------------------------------------


def test_sessions_tools_tokens_and_cost_are_exported():
    agentops.record_session(
        "s-1",
        "acme",
        [
            AgentStep("recall_memory", ok=True, input_tokens=100, output_tokens=20, cost_usd=0.01),
            AgentStep("platform_status", ok=False, error="boom", cost_usd=0.02),
        ],
        agent="skipper",
    )
    text = exposition.export()
    assert _value(text, 'examlops_agent_sessions_started_total{agent="skipper"}') == 1
    assert _value(text, 'examlops_agent_sessions_ended_total{agent="skipper",outcome="ok"}') == 1
    assert _value(text, 'examlops_agent_tool_calls_total{outcome="ok",tool="recall_memory"}') == 1
    assert (
        _value(text, 'examlops_agent_tool_calls_total{outcome="error",tool="platform_status"}') == 1
    )
    assert _value(text, 'examlops_agent_steps_total{agent="skipper"}') == 2
    assert _value(text, 'examlops_agent_tokens_total{agent="skipper",direction="input"}') == 100
    assert _value(text, 'examlops_agent_cost_usd_total{agent="skipper"}') == pytest.approx(0.03)


def test_an_anomalous_session_is_counted_by_code_and_outcome():
    looping = [AgentStep("search", args={"q": "x"}) for _ in range(3)]
    agentops.record_session("s-loop", "acme", looping, agent="skipper")
    text = exposition.export()
    assert _value(text, 'examlops_agent_anomalies_total{agent="skipper",code="loop"}') == 1
    assert (
        _value(text, 'examlops_agent_sessions_ended_total{agent="skipper",outcome="anomaly"}') == 1
    )


def test_no_series_carries_a_session_id_or_tenant_label():
    """Cardinality discipline: a per-session label would grow without bound."""
    for i in range(5):
        agentops.record_session(f"unique-session-{i}", f"tenant-{i}", [AgentStep("t")], agent="a")
    text = exposition.export()
    assert "unique-session" not in text and "tenant-" not in text
    label_names = set(re.findall(r"[{,](\w+)=", text))
    assert label_names <= {"agent", "outcome", "tool", "direction", "code", "event", "le"}


def test_session_duration_histogram_uses_the_real_start_time():
    """A session is flushed when it ends, so without the recorded start it would read as 0 s."""
    rec = SessionRecorder("s-slow", tenant="acme", agent="skipper")
    rec.started_at = time.time() - 30  # began 30 s ago
    rec.add(AgentStep("recall_memory"))
    rec.flush()
    text = exposition.export()
    fam = "examlops_agent_session_duration_seconds"
    assert _value(text, f'{fam}_count{{agent="skipper"}}') == 1
    assert _value(text, f'{fam}_bucket{{agent="skipper",le="15"}}') == 0
    assert _value(text, f'{fam}_bucket{{agent="skipper",le="60"}}') == 1
    assert 25 <= _value(text, f'{fam}_sum{{agent="skipper"}}') <= 40
    assert text.count(f"# TYPE {fam} histogram") == 1
    assert f"# TYPE {fam}_bucket" not in text  # HELP/TYPE belong to the family, once


def test_an_upsert_never_moves_a_sessions_start():
    from examlops import platform_db

    platform_db.record_agent_session("s-up", started_at="2026-01-01 00:00:00", ended=True)
    platform_db.record_agent_session("s-up", started_at="2026-06-06 00:00:00", ended=True)
    assert platform_db.list_agent_sessions()[0]["started_at"] == "2026-01-01 00:00:00"


def test_an_empty_platform_exports_no_agent_series():
    assert "examlops_agent_" not in exposition.export()


# -- decision 4: warn before abort ---------------------------------------------------------------


def test_the_breaker_warns_before_it_aborts_a_loop():
    br = AgentCircuitBreaker(loop_threshold=4, warn_ratio=0.5)
    br.guard(AgentStep("search", args={"q": "x"}))
    assert br.warnings == []
    br.guard(AgentStep("search", args={"q": "x"}))  # 2 == ceil(0.5*4): soft threshold
    assert [w.code for w in br.warnings] == ["loop_warning"]
    assert not br.tripped()
    br.guard(AgentStep("search", args={"q": "x"}))
    with pytest.raises(CircuitBreakerTripped):
        br.guard(AgentStep("search", args={"q": "x"}))  # 4th: the hard abort, unchanged
    assert br.tripped_by.code == "loop"


def test_a_warning_fires_once_per_kind_not_once_per_step():
    br = AgentCircuitBreaker(step_threshold=10, warn_ratio=0.5)
    for i in range(9):
        br.guard(AgentStep(f"tool{i}"))
    assert [w.code for w in br.warnings] == ["step_blowup_warning"]


def test_cost_warning_precedes_the_cost_abort():
    br = AgentCircuitBreaker(cost_budget=1.0, warn_ratio=0.5, abort_on_cost=True)
    br.guard(AgentStep("llm", cost_usd=0.6))
    assert [w.code for w in br.warnings] == ["cost_warning"]
    with pytest.raises(CircuitBreakerTripped):
        br.guard(AgentStep("llm", cost_usd=0.6))


def test_warn_ratio_zero_restores_the_old_behaviour_exactly():
    events: list[tuple[str, str]] = []
    br = AgentCircuitBreaker(
        loop_threshold=3, warn_ratio=0.0, on_event=lambda k, a: events.append((k, a.code))
    )
    br.guard(AgentStep("search", args={"q": "x"}))
    br.guard(AgentStep("search", args={"q": "x"}))
    assert br.warnings == []
    with pytest.raises(CircuitBreakerTripped):
        br.guard(AgentStep("search", args={"q": "x"}))
    assert events == [("tripped", "loop")]  # only the trip, as before


def test_the_ratio_is_configurable_from_the_environment(monkeypatch):
    assert agentops.warn_ratio_from_env() == agentops.WARN_RATIO_DEFAULT
    monkeypatch.setenv(agentops.WARN_RATIO_ENV, "0.5")
    assert agentops.warn_ratio_from_env() == 0.5
    monkeypatch.setenv(agentops.WARN_RATIO_ENV, "garbage")
    assert agentops.warn_ratio_from_env() == agentops.WARN_RATIO_DEFAULT
    monkeypatch.setenv(agentops.WARN_RATIO_ENV, "7")  # a ratio at or above 1 would never warn early
    assert agentops.warn_ratio_from_env() < 1.0
    assert AgentCircuitBreaker().warn_ratio == agentops.warn_ratio_from_env()


def test_a_broken_observer_does_not_change_the_abort_decision():
    def boom(_kind, _anomaly):
        raise RuntimeError("observer down")

    br = AgentCircuitBreaker(loop_threshold=2, warn_ratio=0.0, on_event=boom)
    br.guard(AgentStep("t", args="a"))
    with pytest.raises(CircuitBreakerTripped):
        br.guard(AgentStep("t", args="a"))


def test_warnings_and_trips_are_audited_and_counted():
    from examlops import platform_db

    br = AgentCircuitBreaker(loop_threshold=3, warn_ratio=0.6, session_id="s-audit")
    br.guard(AgentStep("search", args="q"))
    br.guard(AgentStep("search", args="q"))
    with pytest.raises(CircuitBreakerTripped):
        br.guard(AgentStep("search", args="q"))
    with platform_db.get_db() as conn:
        actions = [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_events WHERE source='agentops'")
        ]
    assert sorted(actions) == ["agent_breaker_tripped", "agent_breaker_warning"]
    text = exposition.export()
    assert (
        _value(text, 'examlops_agent_breaker_events_total{code="loop_warning",event="warning"}')
        == 1
    )
    assert _value(text, 'examlops_agent_breaker_events_total{code="loop",event="tripped"}') == 1


def test_a_silent_breaker_can_skip_the_audit():
    br = AgentCircuitBreaker(loop_threshold=2, warn_ratio=0.5, emit_audit=False)
    br.guard(AgentStep("t", args="a"))
    with pytest.raises(CircuitBreakerTripped):
        br.guard(AgentStep("t", args="a"))
    assert "examlops_agent_breaker_events_total" not in exposition.export()


# -- decision 2: OTel spans an OTLP consumer can read --------------------------------------------


@pytest.fixture
def spans(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    yield exporter
    exporter.clear()


def test_agent_and_tool_spans_carry_the_attributes_langfuse_and_phoenix_read(spans):
    """The OTLP-native consumers read the ``gen_ai.*`` conventions; OTel stays the source of truth.

    Langfuse and Arize Phoenix ingest standard OTLP, so nothing here is specific to either:
    the assertion is that the spans this platform already emits name their operation, model and
    token usage under the semantic-convention keys.
    """
    from examlops.telemetry import genai

    with genai.genai_span("agent", system="skipper", model="llama3.1:8b", tenant="acme") as agent:
        genai.record_usage(agent, model="llama3.1:8b", input_tokens=120, output_tokens=30)
        with genai.genai_span("tool", system="skipper", model="recall_memory", tenant="acme"):
            pass
    by_name = {s.name: dict(s.attributes) for s in spans.get_finished_spans()}
    agent_attrs = by_name["gen_ai.agent llama3.1:8b"]
    assert agent_attrs["gen_ai.operation.name"] == "agent"
    assert agent_attrs["gen_ai.system"] == "skipper"
    assert agent_attrs["gen_ai.request.model"] == "llama3.1:8b"
    assert agent_attrs["gen_ai.usage.input_tokens"] == 120
    assert agent_attrs["gen_ai.usage.output_tokens"] == 30
    assert by_name["gen_ai.tool recall_memory"]["gen_ai.operation.name"] == "tool"
    # the tool span is a child of the agent span, so a consumer renders one trace tree
    finished = {s.name: s for s in spans.get_finished_spans()}
    tool, agent_span = finished["gen_ai.tool recall_memory"], finished["gen_ai.agent llama3.1:8b"]
    assert tool.parent is not None and tool.parent.span_id == agent_span.context.span_id


def test_the_latest_semconv_opt_in_renames_the_operations_consumers_expect(spans, monkeypatch):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    from examlops.telemetry import genai

    with genai.genai_span("agent", system="skipper", model="m"):
        pass
    attrs = dict(spans.get_finished_spans()[0].attributes)
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.provider.name"] == "skipper"
    assert "gen_ai.system" not in attrs


def test_tracing_stays_off_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    from examlops.telemetry import genai

    with genai.genai_span("agent", system="skipper", model="m") as span:
        assert span.is_noop


def test_a_lost_breaker_audit_is_counted_and_does_not_change_the_abort(monkeypatch):
    from examlops.data import audit

    audit.reset_dropped_audit_events()

    def _boom(*_a, **_k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "write_audit_event", _boom)
    br = AgentCircuitBreaker(loop_threshold=2, warn_ratio=0.0)
    br.guard(AgentStep("t", args="a"))
    with pytest.raises(CircuitBreakerTripped):  # the loss does not stop the abort...
        br.guard(AgentStep("t", args="a"))
    assert audit.dropped_audit_events().get("agent_breaker_tripped") == 1  # ...and is not hidden

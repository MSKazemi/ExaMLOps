"""ADR 0021 decision 4 → ADR 0023 (C6): agent SLOs are C6 SLO objects with burn-rate alerts.

Before this, the agent's alerts were three hand-written Prometheus rules on the exported series —
no SLO object, no error budget, no burn rate, no breach audit. The ``c4`` SLI source makes an
agent (``agent_sessions.agent``) a first-class C6 subject: ``exa slo set skipper tools --source c4
--query tool_success`` then ``exa slo ingest skipper`` keeps an error budget over the recorded tool
calls, audits the moment it is spent, and ``exa slo generate`` emits multi-window burn-rate rules
over the series ``exa slo export-metrics`` publishes.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import agentops, slo  # noqa: E402
from examlops.agentops import AgentStep  # noqa: E402
from examlops.data.agent import agent_sli  # noqa: E402
from examlops.platform_db import get_db, init_db, upsert_slo_spec  # noqa: E402
from examlops.telemetry import exposition  # noqa: E402


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


def _spec(name: str, query: str | None, *, agent: str = "skipper", tenant: str = "default"):
    upsert_slo_spec(agent, name, tenant=tenant, sli_source="c4", sli_query=query, target=0.9)


def _session(sid: str, oks: list[bool], *, agent: str = "skipper", tenant: str = "default"):
    steps = [
        AgentStep(f"tool{i}", args={"i": i}, ok=ok, error=None if ok else "boom")
        for i, ok in enumerate(oks)
    ]
    return agentops.record_session(sid, tenant, steps, agent=agent)


def _row(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


def _stamp(delta_s: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_s)).strftime("%Y-%m-%d %H:%M:%S")


# ── the source ingests, per agent and per tenant ─────────────────────────────


def test_tool_success_counts_the_agents_tool_calls():
    _spec("tools", "tool_success")
    _session("s1", [True, True, False])
    _session("s2", [True])

    row = _row(slo.ingest_slis("skipper"), "tools")

    assert row["ingested"] is True
    assert (row["good"], row["total"]) == (3.0, 4.0)


def test_another_agent_and_another_tenant_are_not_counted():
    _spec("tools", "tool_success")
    _session("mine", [True, True])
    _session("other-agent", [False, False], agent="helper")
    _session("other-tenant", [False, False], tenant="acme")

    row = _row(slo.ingest_slis("skipper"), "tools")

    assert (row["good"], row["total"]) == (2.0, 2.0)


def test_one_tool_can_be_pinned():
    _spec("t1", "tool_success:tool1")
    _session("s1", [True, False, True])  # tool0 ok, tool1 failed, tool2 ok

    row = _row(slo.ingest_slis("skipper"), "t1")

    assert (row["good"], row["total"]) == (0.0, 1.0)


def test_session_ok_counts_sessions_not_calls():
    _spec("clean", "session_ok")
    _session("good", [True, True])
    looping = [AgentStep("search", args={"q": "x"}) for _ in range(3)]  # a loop anomaly
    agentops.record_session("loop", "default", looping, agent="skipper")

    row = _row(slo.ingest_slis("skipper"), "clean")

    assert (row["good"], row["total"]) == (1.0, 2.0)


# ── each event is counted once ───────────────────────────────────────────────


def test_a_second_ingest_counts_only_new_calls():
    _spec("tools", "tool_success")
    _session("s1", [True, False])
    first = _row(slo.ingest_slis("skipper"), "tools")
    again = _row(slo.ingest_slis("skipper"), "tools")
    _session("s2", [True])
    third = _row(slo.ingest_slis("skipper"), "tools")

    assert (first["good"], first["total"]) == (1.0, 2.0)
    assert again["ingested"] is False and again["up_to_date"] is True
    assert (third["good"], third["total"]) == (1.0, 1.0)
    st = slo.slo_status("skipper", "tools")[0]
    assert st.n == 3  # 3 calls, never re-counted


def test_calls_whose_session_is_not_flushed_yet_hold_the_watermark_back():
    """A session writes its tool calls before its summary row. An ingest in between must not
    advance past calls it cannot attribute yet, or they are lost for good."""
    from examlops.data.agent import record_agent_session, record_agent_tool_call

    _spec("tools", "tool_success")
    _session("done", [True])
    record_agent_tool_call("inflight", "tool0", ok=False)  # summary not written yet
    _session("later", [True])  # a later, finished session

    first = _row(slo.ingest_slis("skipper"), "tools")
    record_agent_session("inflight", agent="skipper", tool_calls=1, errors=1, ended=True)
    second = _row(slo.ingest_slis("skipper"), "tools")

    assert (first["good"], first["total"]) == (1.0, 1.0)  # stops below the in-flight call
    assert (second["good"], second["total"]) == (1.0, 2.0)  # then counts it and the later one
    assert slo.slo_status("skipper", "tools")[0].n == 3


def test_an_abandoned_orphan_does_not_freeze_the_sli_forever():
    _spec("tools", "tool_success")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tool_calls (session_id, tenant, tool, ok, ts) VALUES (?,?,?,?,?)",
            ("crashed", "default", "tool0", 0, _stamp(-3600)),
        )
    _session("after", [True, True])

    row = _row(slo.ingest_slis("skipper"), "tools")

    assert (row["good"], row["total"]) == (2.0, 2.0)


def test_agent_sli_rejects_an_unknown_objective():
    with pytest.raises(ValueError, match="objective"):
        agent_sli("skipper", tenant="default", since="", settle_before="", objective="vibes")


# ── what cannot be measured says why ─────────────────────────────────────────


@pytest.mark.parametrize("query", [None, "", "happiness", "session_ok:tool1"])
def test_a_missing_or_wrong_query_is_refused_with_the_accepted_forms(query):
    _spec("bad", query)
    _session("s1", [True])

    row = _row(slo.ingest_slis("skipper"), "bad")

    assert row["ingested"] is False
    assert "tool_success" in row["reason"] and "session_ok" in row["reason"]


def test_an_agent_with_no_sessions_is_unmeasured_not_healthy():
    _spec("tools", "tool_success", agent="ghost")

    row = _row(slo.ingest_slis("ghost"), "tools")

    assert row["ingested"] is False and "no ended sessions" in row["reason"]
    assert slo.slo_status("ghost", "tools")[0].measured is False


def test_c4_is_a_supported_source():
    assert "c4" in slo.SUPPORTED_SOURCES and "c4" not in slo.UNSUPPORTED_SOURCES


# ── the C6 machinery now applies to the agent ────────────────────────────────


def test_spending_the_agents_error_budget_is_audited_as_a_breach():
    _spec("tools", "tool_success")
    _session("s1", [False, False, True, True])  # 50% against a 90% target

    row = _row(slo.ingest_slis("skipper"), "tools")

    assert row["breached"] is True
    with get_db() as conn:
        audits = conn.execute(
            "SELECT target FROM audit_events WHERE action='slo_breached'"
        ).fetchall()
    assert [a["target"] for a in audits] == ["skipper/tools"]
    st = slo.slo_status("skipper", "tools")[0]
    assert st.burn_rate == pytest.approx(5.0)  # 0.5 observed error / 0.1 allowed


def test_generated_burn_rate_rules_range_over_the_exported_series():
    """The c4 query (`tool_success`) is not PromQL; the rules must use the series that
    `exa slo export-metrics` actually publishes, or they could never fire."""
    from examlops.data.governance import get_slo_spec

    _spec("tools", "tool_success")
    _session("s1", [True, False])
    slo.ingest_slis("skipper")

    rules = slo.generate_rules(get_slo_spec("skipper", "tools", "default"))
    exported = exposition.export("skipper")

    series = 'examlops_slo_sli{model="skipper",slo="tools",tenant="default"}'
    recording = rules.groups[0]["rules"][0]
    assert recording["expr"] == series
    assert "tool_success" not in str(rules.groups)
    assert series.replace("examlops_slo_sli", "examlops_slo_sli", 1) in exported
    alerts = rules.groups[1]["rules"]
    assert len(alerts) == len(slo.BURN_RATE_WINDOWS)
    # Every series an alert selects is one the export actually publishes, for this SLO.
    for a in alerts:
        for metric in set(re.findall(r"(examlops_slo_\w+)\{", a["expr"])):
            assert f'{metric}{{model="skipper",slo="tools",tenant="default"}}' in exported


def test_burn_rate_alerts_use_windowed_counters_not_the_window_long_gauge():
    """A burn rate is the error ratio over the *short* window. `examlops_slo_sli` is the ratio
    over the SLO's whole window, so averaging it over 5m cannot see a fresh burn: a month of good
    traffic followed by an hour at 50% errors reads as ~1% on the gauge. Replayed through promtool
    (2026-09-25), the counter form of the 99% fast-burn alert fires at 2h05 on exactly that input
    and stays silent at 0h30; the gauge form cannot fire on it."""
    from examlops.data.governance import get_slo_spec

    _spec("tools", "tool_success")  # target 0.9
    rules = slo.generate_rules(get_slo_spec("skipper", "tools", "default"))
    for alert, (short_w, long_w, *_rest) in zip(
        rules.groups[1]["rules"], slo.BURN_RATE_WINDOWS, strict=True
    ):
        expr = alert["expr"]
        assert "examlops_slo_sli" not in expr and "avg_over_time" not in expr
        for w in (short_w, long_w):
            assert "increase(examlops_slo_good_total{" in expr and f"[{w}]" in expr
            assert "increase(examlops_slo_events_total{" in expr


def test_the_exported_counters_are_the_all_time_sums_and_only_grow():
    _spec("tools", "tool_success")
    _session("s1", [True, False])
    slo.ingest_slis("skipper")
    first = exposition.export("skipper")
    _session("s2", [False, False, True])
    slo.ingest_slis("skipper")
    second = exposition.export("skipper")

    labels = '{model="skipper",slo="tools",tenant="default"}'
    assert f"examlops_slo_events_total{labels} 2.0" in first
    assert f"examlops_slo_good_total{labels} 1.0" in first
    assert f"examlops_slo_events_total{labels} 5.0" in second
    assert f"examlops_slo_good_total{labels} 2.0" in second
    assert "# TYPE examlops_slo_events_total counter" in second


# ── session_ok counts turns, not threads ─────────────────────────────────────


def test_session_ok_counts_every_turn_of_a_thread():
    """Skipper's session id is the conversation thread, so a second turn overwrites the first
    turn's status. Reading `agent_sessions` counted the thread once, with the *last* status —
    the first turn's loop anomaly vanished from the SLI."""
    _spec("clean", "session_ok")
    looping = [AgentStep("search", args={"q": "x"}) for _ in range(3)]
    agentops.record_session("thread-1", "default", looping, agent="skipper")  # turn 1: anomaly
    _session("thread-1", [True])  # turn 2 of the same thread: ok

    row = _row(slo.ingest_slis("skipper"), "clean")

    assert (row["good"], row["total"]) == (1.0, 2.0)


def test_session_ok_does_not_depend_on_how_often_it_is_ingested():
    def run(ingest_between: bool) -> float:
        _spec("clean", "session_ok", agent=f"a{ingest_between}")
        agent = f"a{ingest_between}"
        agentops.record_session(
            f"t-{agent}", "default", [AgentStep("s", args={"q": 1}) for _ in range(3)], agent=agent
        )
        if ingest_between:
            slo.ingest_slis(agent)
        _session(f"t-{agent}", [True], agent=agent)
        slo.ingest_slis(agent)
        return slo.slo_status(agent, "clean")[0].sli

    assert run(True) == run(False) == pytest.approx(0.5)


def test_session_ok_counts_a_turn_that_made_no_tool_call():
    _spec("clean", "session_ok")
    agentops.record_session("no-tools", "default", [], agent="skipper")

    row = _row(slo.ingest_slis("skipper"), "clean")

    assert (row["good"], row["total"]) == (1.0, 1.0)


def test_session_ok_is_tenant_and_agent_scoped_and_counted_once():
    _spec("clean", "session_ok")
    _session("mine", [True])
    _session("other-agent", [False], agent="helper")
    _session("other-tenant", [False], tenant="acme")

    first = _row(slo.ingest_slis("skipper"), "clean")
    again = _row(slo.ingest_slis("skipper"), "clean")

    assert (first["good"], first["total"]) == (1.0, 1.0)
    assert again["ingested"] is False and again["up_to_date"] is True


def test_a_prometheus_spec_still_uses_its_own_promql():
    rules = slo.generate_rules(
        {"model": "M", "name": "s", "target": 0.99, "sli_source": "prometheus", "sli_query": "up"}
    )
    assert rules.groups[0]["rules"][0]["expr"] == "up"

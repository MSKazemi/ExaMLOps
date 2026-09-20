"""Per-kind unit economics — ADR 0148 decision 4, over a real sqlite platform.db."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops import platform_db as pdb
from examlops.cli.main import app
from examlops.data.agent import record_agent_session
from examlops.data.finops import write_prediction
from examlops.finops import economics as eco


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_ECONOMICS_MIN_SAMPLES", raising=False)
    pdb.init_db()


def _kind(out, name):
    return next(k for k in out["kinds"] if k["kind"] == name)


def _gw(n, cost=0.01, pt=100, ct=50, error=False):
    from examlops.data.gateway import record_gateway_call

    for _ in range(n):
        record_gateway_call(
            None,
            "m",
            cost_usd=cost,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=10.0,
            error=error,
        )


def test_empty_ledgers_give_no_data_never_zero():
    out = eco.economics()
    assert [k["kind"] for k in out["kinds"]] == list(eco.KINDS)
    for k in out["kinds"]:
        assert k["n"] == 0
        assert k["cost_per_unit_usd"]["value"] is None
        assert k["cost_usd"] is None
    assert _kind(out, "generative")["cost_per_unit_usd"]["reason"] == "no_data"
    assert _kind(out, "agentic")["success_rate"] is None


def test_generative_per_1k_tokens_and_per_success():
    _gw(8)  # 8 ok calls: 1200 tokens/8... 150 each
    _gw(2, error=True)
    g = _kind(eco.economics("generative"), "generative")
    assert g["n"] == 10 and g["errors"] == 2 and g["tokens"] == 1500
    assert g["cost_usd"] == pytest.approx(0.10)
    assert g["cost_per_unit_usd"]["value"] == pytest.approx(0.10 / 1500 * 1000)
    assert g["cost_per_success_usd"]["value"] == pytest.approx(0.10 / 8)


def test_below_min_samples_states_no_unit_cost(monkeypatch):
    _gw(3)
    g = _kind(eco.economics("generative"), "generative")
    assert g["cost_per_unit_usd"]["value"] is None
    assert g["cost_per_unit_usd"]["reason"].startswith("insufficient_samples")
    monkeypatch.setenv("EXAMLOPS_ECONOMICS_MIN_SAMPLES", "3")
    assert _kind(eco.economics("generative"), "generative")["cost_per_unit_usd"]["value"]


def test_zero_tokens_is_not_metered_not_zero():
    _gw(6, cost=0.0, pt=0, ct=0)
    g = _kind(eco.economics("generative"), "generative")
    assert g["cost_per_unit_usd"] == {"value": None, "reason": "not_metered"}


def test_reasoning_share_from_reasoning_usage():
    _gw(5)
    pdb.record_reasoning_usage(
        "m", reasoning_tokens=300, output_tokens=100, reasoning_cost=0.3, output_cost=0.1
    )
    g = _kind(eco.economics("generative"), "generative")
    assert g["reasoning_token_share"] == pytest.approx(0.75)
    assert g["reasoning_cost_usd"] == pytest.approx(0.4)


def test_agentic_is_a_lower_bound_and_only_ended_sessions_are_tasks():
    for i in range(6):
        record_agent_session(
            f"s{i}", cost_usd=0.5, status="ok" if i < 4 else "error", tool_calls=3, ended=True
        )
    record_agent_session("open", cost_usd=99.0)  # not ended: no outcome, must not count
    a = _kind(eco.economics("agentic"), "agentic")
    assert a["n"] == 6 and a["open_sessions"] == 1 and a["succeeded"] == 4
    assert a["cost_usd"] == pytest.approx(3.0)
    assert a["cost_per_unit_usd"]["value"] == pytest.approx(0.5)
    assert a["cost_per_success_usd"]["value"] == pytest.approx(3.0 / 4)
    assert a["success_rate"] == pytest.approx(4 / 6)
    assert a["complete"] is False and a["cost_bound"] == "lower"
    unmetered = {c["component"] for c in a["components"] if not c["metered"]}
    assert {"sandbox_seconds", "idle_state_gb_hours", "hot_pool_standby_share"} <= unmetered


def test_agentic_no_successes_gives_no_cost_per_success():
    for i in range(6):
        record_agent_session(f"s{i}", cost_usd=0.5, status="error", ended=True)
    a = _kind(eco.economics("agentic"), "agentic")
    assert a["succeeded"] == 0
    assert a["cost_per_success_usd"] == {"value": None, "reason": "not_metered"}


def test_predictive_has_no_usd_cost_and_counts_predictions():
    for i in range(7):
        write_prediction("m", "Production", f"h{i}", 1.0)
    p = _kind(eco.economics("predictive"), "predictive")
    assert p["n"] == 7 and p["cost_per_unit_usd"]["reason"] == "no_inference_cost_metered"
    assert p["kwh_per_unit"]["value"] is None and p["complete"] is False


def test_predictive_energy_per_prediction_from_rows():
    with pdb.get_db() as conn:
        conn.execute(
            "INSERT INTO inference_energy (model, requests, kwh, co2e_g) VALUES ('m', 10, 0.5, 100)"
        )
    p = _kind(eco.economics("predictive"), "predictive")
    # 10 energy-metered requests but n = predictions (0) -> stated, not fabricated
    assert p["kwh_per_unit"]["value"] == pytest.approx(0.05)
    assert p["co2e_g_per_unit"]["value"] == pytest.approx(10.0)


def test_window_excludes_old_rows():
    _gw(6)
    with pdb.get_db() as conn:
        conn.execute("UPDATE gateway_calls SET ts='2000-01-01 00:00:00'")
    assert _kind(eco.economics("generative", days=7), "generative")["n"] == 0
    assert _kind(eco.economics("generative"), "generative")["n"] == 6


def test_bad_kind_and_days():
    with pytest.raises(ValueError):
        eco.economics("quantum")
    with pytest.raises(ValueError):
        eco.economics(days=0)


def test_kinds_are_never_summed():
    out = eco.economics()
    assert "total" not in out and "cost_usd" not in out and "never summed" in out["note"]


def test_cli_json_and_bad_kind(monkeypatch):
    _gw(6)
    r = CliRunner().invoke(app, ["--json", "finops", "economics", "--kind", "generative"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["kinds"][0]["n"] == 6
    bad = CliRunner().invoke(app, ["finops", "economics", "--kind", "nope"])
    assert bad.exit_code == 2
    table = CliRunner().invoke(app, ["finops", "economics"])
    assert table.exit_code == 0 and "agentic" in table.output

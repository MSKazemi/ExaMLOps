"""SLOSpec by kind + the opt-in `slo` promotion gate (ADR 0148 decisions 3 and 4/verification 3).

Real code paths on a real sqlite ``platform.db`` (the suite's per-test one): the real validators,
evaluators, stores, CLI, policy gate, ``exa pipeline promote``, autopilot cycle and agent-version
alias move. Only MLflow's HTTP client and the autopilot's MLflow helpers are faked.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import agent_versions as av  # noqa: E402
from examlops.cli.commands import autopilot_cmd  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import agent as agent_data  # noqa: E402
from examlops.data import gateway as gw_data  # noqa: E402
from examlops.data import slo_kind_specs as store  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result, record_judge_calibration  # noqa: E402
from examlops.evaluation import calibration as cal_mod  # noqa: E402
from examlops.evaluation.calibration import wilson_interval  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    init_db,
    set_autopilot_config,
    set_eval_gate,
    set_promotion_rule,
)
from examlops.slo import specs  # noqa: E402
from examlops.slo.pairs import set_pair  # noqa: E402

runner = CliRunner()
DIGEST = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setenv("EXAMLOPS_SLO_SPEC_MIN_SAMPLES", "5")
    monkeypatch.delenv("EXAMLOPS_POLICY_GATES", raising=False)
    monkeypatch.delenv("EXAMLOPS_POLICY_ENGINE", raising=False)
    monkeypatch.delenv("EXAMLOPS_SLO_SPEC_VERDICT_MAX_AGE_HOURS", raising=False)
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    monkeypatch.setattr("examlops.secrets.get_secret", _no_secret)
    init_db()
    yield tmp_path


def _no_secret(*_a, **_k):
    raise LookupError("no secret store in this test")


def _arm(mode: str, tmp_path=None):
    """Arm the gate through the real env variable."""
    import os

    os.environ["EXAMLOPS_POLICY_GATES"] = f"slo={mode}"


@pytest.fixture(autouse=True)
def _unarm():
    yield
    import os

    os.environ.pop("EXAMLOPS_POLICY_GATES", None)


def _events(prefix):
    return [e for e in export_audit_events() if str(e["action"]).startswith(prefix)]


def _cal(judge, **over):
    base = dict(
        judge=judge,
        version="v1",
        at="2026-09-21T00:00:00+00:00",
        kappa=0.71,
        kappa_ci=(0.62, 0.80),
        position_bias=0.02,
        test_retest=0.88,
        benchmarks=["mt-bench-sample", "gsm8k-sample"],
        families=["correctness", "preference"],
        replications=3,
        paradox_flag=False,
        sensitivity=0.9,
        specificity=0.85,
        n=200,
    )
    base.update(over)
    return cal_mod.JudgeCalibration(**base)


PRED = {"latency_p99_ms": 300.0, "error_rate": 0.01, "availability": 0.99}


def _pred_obs(latency=300.0, n=20, requests=1000, errors=10, good=990, total=1000):
    return {
        "latency_ms": [latency] * n,
        "requests": requests,
        "errors": errors,
        "availability_good": good,
        "availability_total": total,
    }


def _tasks(n=100, ok=100, jct=10.0, cost=0.1, intervened=0):
    return [
        {"success": i < ok, "jct_s": jct, "cost_usd": cost, "intervened": i < intervened}
        for i in range(n)
    ]


# ── validation: every kind refuses what its shape does not allow ────────────────────────────


@pytest.mark.parametrize(
    "kind, fields, fragment",
    [
        ("batchy", {"error_rate": 0.1}, "kind must be one of"),
        ("predictive", {}, "at least one of"),
        ("predictive", {"latency_p99_ms": 0}, "latency_p99_ms"),
        ("predictive", {"latency_p99_ms": float("nan")}, "must be a number"),
        ("predictive", {"latency_p99_ms": True}, "must be a number"),
        ("predictive", {"error_rate": 1.0}, "error_rate"),
        ("predictive", {"error_rate": -0.1}, "error_rate"),
        ("predictive", {"availability": 0.0}, "availability"),
        ("predictive", {"availability": 1.01}, "availability"),
        ("predictive", {"pair": "x"}, "does not take pair"),
        ("predictive", {"task_success": 0.9}, "does not take task_success"),
        ("generative", {"goodput_target": 0.9}, "needs pair"),
        ("generative", {"pair": "nope"}, "no paired SLO 'nope'"),
        ("agentic", {"judge": "j"}, "needs task_success"),
        ("agentic", {"task_success": 0.9}, "needs judge"),
        ("agentic", {"task_success": 0.0, "judge": "j"}, "task_success"),
        ("agentic", {"task_success": 0.9, "judge": " "}, "non-empty"),
        ("agentic", {"task_success": 0.9, "judge": "j", "jct_p50_s": 9, "jct_p95_s": 8}, "p50"),
        ("agentic", {"task_success": 0.9, "judge": "j", "intervention_rate": 1.0}, "intervention"),
        ("agentic", {"task_success": 0.9, "judge": "j", "ttft": 1}, "does not take ttft"),
    ],
)
def test_validation_refusals(kind, fields, fragment):
    with pytest.raises(specs.SLOSpecError) as exc:
        specs.validate("svc", kind, fields)
    assert fragment in str(exc.value)


def test_generative_needs_a_p99_pair():
    set_pair("chat", "p90", ttft_ms=200, tpot_ms=50, percentile=90)
    with pytest.raises(specs.SLOSpecError, match="p90"):
        specs.validate("chat", "generative", {"pair": "p90"})
    set_pair("chat", "p99", ttft_ms=200, tpot_ms=50)
    assert specs.validate("chat", "generative", {"pair": "p99", "goodput_target": 0.95}) == {
        "pair": "p99",
        "goodput_target": 0.95,
    }


def test_valid_specs_normalise_and_boundaries_are_accepted():
    assert specs.validate("m", "predictive", {"error_rate": 0, "availability": 1}) == {
        "error_rate": 0.0,
        "availability": 1.0,
    }
    out = specs.validate("a", "agentic", {"task_success": 1, "judge": "j", "jct_p50_s": 5})
    assert out == {"task_success": 1.0, "judge": "j", "jct_p50_s": 5.0}


# ── predictive evaluator ────────────────────────────────────────────────────────────────────


def test_predictive_exactly_at_every_threshold_is_met():
    v = specs.evaluate("predictive", PRED, _pred_obs(latency=300.0, errors=10, good=990))
    assert v.verdict == "met" and v.passed and v.reasons == []


@pytest.mark.parametrize(
    "obs, dim",
    [
        (_pred_obs(latency=300.01), "latency_p99_ms"),
        (_pred_obs(errors=11), "error_rate"),
        (_pred_obs(good=989), "availability"),
    ],
)
def test_predictive_just_past_a_threshold_is_violated(obs, dim):
    v = specs.evaluate("predictive", PRED, obs)
    assert v.verdict == "violated" and not v.passed
    assert [d.name for d in v.dimensions if d.verdict == "violated"] == [dim]


def test_predictive_no_data_is_no_verdict_never_met():
    v = specs.evaluate("predictive", PRED, {})
    assert v.verdict == "no_verdict" and not v.passed
    assert all(d.verdict == "no_verdict" for d in v.dimensions)


def test_predictive_one_undecidable_dimension_blocks_met_but_a_violation_still_wins():
    partial = {"latency_ms": [100.0] * 20}  # latency decidable, error/availability not
    assert specs.evaluate("predictive", PRED, partial).verdict == "no_verdict"
    assert specs.evaluate("predictive", PRED, {"latency_ms": [999.0] * 20}).verdict == "violated"


def test_predictive_malformed_observations_are_excluded_not_counted_as_good():
    bad = [float("nan"), -1.0, True, None, "x"] + [100.0] * 4  # only 4 valid, need 5
    v = specs.evaluate("predictive", {"latency_p99_ms": 300.0}, {"latency_ms": bad})
    assert v.verdict == "no_verdict" and v.dimensions[0].n == 4
    # more requests than errors makes no sense: refuse to decide rather than pass
    v = specs.evaluate("predictive", {"error_rate": 0.5}, {"requests": 10, "errors": 20})
    assert v.verdict == "no_verdict"


# ── generative evaluator (wraps the pair evaluator) ─────────────────────────────────────────


def _gen_setup(goodput=None):
    set_pair("chat", "p99", ttft_ms=200, tpot_ms=50)
    fields = {"pair": "p99"}
    if goodput is not None:
        fields["goodput_target"] = goodput
    return specs.set_spec("chat", "generative", fields)


def test_generative_delegates_to_the_pair_and_absence_is_not_a_pass(env):
    _gen_setup(goodput=0.99)
    ok = specs.check_spec("chat", "generative", samples=[[200.0, 50.0]] * 30)
    assert ok.verdict == "met" and {d.name for d in ok.dimensions} == {
        "ttft_p99_ms",
        "tpot_p99_ms",
        "goodput",
    }
    slow = specs.check_spec("chat", "generative", samples=[[200.01, 50.0]] * 30)
    assert slow.verdict == "violated"
    none = specs.check_spec("chat", "generative")
    assert none.verdict == "no_verdict" and any("not persist" in r for r in none.reasons)


def test_generative_goodput_below_target_violates_even_when_the_pair_holds(env):
    _gen_setup(goodput=1.0)
    # 1 slow of 200 -> p99 still fine, attainment 0.995 < 1.0
    samples = [[100.0, 20.0]] * 199 + [[999.0, 20.0]]
    v = specs.check_spec("chat", "generative", samples=samples)
    assert v.verdict == "violated"
    assert [d.name for d in v.dimensions if d.verdict == "violated"] == ["goodput"]


def test_generative_spec_whose_pair_was_removed_is_no_verdict(env):
    _gen_setup()
    from examlops.data import slo_pairs

    slo_pairs.delete("chat", "p99")
    v = specs.check_spec("chat", "generative", samples=[[1.0, 1.0]] * 30)
    assert v.verdict == "no_verdict" and "not declared" in v.reasons[0]


# ── agentic evaluator ───────────────────────────────────────────────────────────────────────

AGENTIC = {
    "task_success": 0.9,
    "judge": "good-judge",
    "jct_p50_s": 10.0,
    "jct_p95_s": 10.0,
    "intervention_rate": 0.1,
    "cost_per_task_p95_usd": 0.1,
}


def test_agentic_boundaries_and_wilson_lower_bound():
    lo, _ = wilson_interval(100, 100)
    spec = {**AGENTIC, "task_success": lo}
    v = specs.evaluate("agentic", spec, {"tasks": _tasks(intervened=10)}, judge_eligible=(True, []))
    assert v.verdict == "met", v.reasons  # exactly at every threshold, including the interval bound
    spec["task_success"] = lo + 1e-9
    v = specs.evaluate("agentic", spec, {"tasks": _tasks(intervened=10)}, judge_eligible=(True, []))
    assert v.verdict == "violated" and "Wilson" in " ".join(v.reasons)


def test_agentic_small_n_with_perfect_record_is_not_enough_evidence():
    # 3 of 3 successes, but need 5 judged tasks: no verdict, not a pass
    v = specs.evaluate(
        "agentic",
        {"task_success": 0.5, "judge": "j"},
        {"tasks": _tasks(n=3, ok=3)},
        judge_eligible=(True, []),
    )
    assert v.verdict == "no_verdict"


def test_agentic_uncalibrated_or_ineligible_judge_is_no_verdict():
    obs = {"tasks": _tasks()}
    spec = {"task_success": 0.5, "judge": "j"}
    unmeasured = specs.evaluate("agentic", spec, obs, judge_eligible=None)
    assert unmeasured.verdict == "no_verdict" and "no calibration" in unmeasured.reasons[0]
    bad = specs.evaluate("agentic", spec, obs, judge_eligible=(False, ["position_bias"]))
    assert bad.verdict == "no_verdict" and "position_bias" in bad.reasons[0]


def test_agentic_each_secondary_objective_can_violate():
    ok = _tasks(intervened=10)
    e = lambda t: specs.evaluate(  # noqa: E731
        "agentic", AGENTIC, {"tasks": t}, judge_eligible=(True, [])
    )
    assert e(ok).verdict == "met"
    assert e(_tasks(intervened=11)).verdict == "violated"
    assert e(_tasks(jct=10.5, intervened=10)).verdict == "violated"
    assert e(_tasks(cost=0.11, intervened=10)).verdict == "violated"


def test_agentic_unjudged_tasks_do_not_count_as_successes():
    tasks = [{"jct_s": 1.0} for _ in range(50)]  # no `success` at all
    v = specs.evaluate(
        "agentic", {"task_success": 0.5, "judge": "j"}, {"tasks": tasks}, judge_eligible=(True, [])
    )
    assert v.verdict == "no_verdict" and v.dimensions[0].n == 0


# ── store, versioning, live sources ─────────────────────────────────────────────────────────


def test_set_is_versioned_and_idempotent_and_tenant_scoped():
    s1 = specs.set_spec("m", "predictive", {"error_rate": 0.01})
    assert s1[:2] == ("created", 1)
    assert specs.set_spec("m", "predictive", {"error_rate": 0.01})[:2] == ("unchanged", 1)
    assert specs.set_spec("m", "predictive", {"error_rate": 0.02})[:2] == ("updated", 2)
    specs.set_spec("m", "predictive", {"error_rate": 0.5}, tenant="acme")
    assert store.get("m", "predictive")["version"] == 2
    assert store.get("m", "predictive", "acme")["spec"] == {"error_rate": 0.5}
    assert [r["tenant"] for r in store.list_specs(servable="m")] == ["acme", "default"]
    assert store.get("m", "agentic") is None


def test_undeclared_spec_is_no_verdict_never_met():
    v = specs.check_spec("ghost", "predictive")
    assert v.verdict == "no_verdict" and "no predictive SLOSpec declared" in v.reasons[0]


def _calls(model, n, latency, errors=0):
    for i in range(n):
        gw_data.record_gateway_call(
            None, model, latency_ms=latency, error=i < errors, prompt_tokens=1
        )


def test_predictive_live_source_is_gateway_calls():
    specs.set_spec("live-m", "predictive", {"latency_p99_ms": 300, "error_rate": 0.1})
    assert specs.check_spec("live-m", "predictive").verdict == "no_verdict"  # no calls yet
    _calls("live-m", 20, 300.0, errors=2)  # errors excluded from latency, exactly-at passes
    v = specs.check_spec("live-m", "predictive")
    assert v.verdict == "met", v.reasons and v.dimensions
    _calls("live-m", 10, 900.0)  # 10 slow of 28 successes: p99 violated
    assert specs.check_spec("live-m", "predictive").verdict == "violated"


def test_predictive_live_data_is_not_used_for_another_tenant():
    specs.set_spec("t-m", "predictive", {"latency_p99_ms": 300}, tenant="acme")
    _calls("t-m", 20, 10.0)
    v = specs.check_spec("t-m", "predictive", tenant="acme")
    assert v.verdict == "no_verdict" and any("no tenant" in r for r in v.reasons)


def test_agentic_live_source_covers_jct_and_cost_but_not_success(env):
    record_judge_calibration(_cal("good-judge"))
    specs.set_spec(
        "skip",
        "agentic",
        {"task_success": 0.5, "judge": "good-judge", "jct_p95_s": 1e6, "cost_per_task_p95_usd": 1},
    )
    for i in range(6):
        agent_data.record_agent_session(
            f"s{i}",
            agent="skip",
            cost_usd=0.5,
            status="ok",
            ended=True,
            started_at="2026-09-21 00:00:00",
        )
    v = specs.check_spec("skip", "agentic", window_days=3650)
    by = {d.name: d for d in v.dimensions}
    assert by["cost_per_task_p95_usd"].verdict == "met"
    assert by["task_success"].verdict == "no_verdict" and v.verdict == "no_verdict"
    assert any("not recorded" in r for r in v.reasons)


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────


def _x(*args):
    return runner.invoke(app, ["--json", "slo", "spec", *args])


def test_cli_set_show_list_check_json_and_exit_codes(tmp_path):
    r = _x("set", "jpcp", "--kind", "predictive", "--latency-p99-ms", "300")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["state"] == "created"
    assert (
        json.loads(_x("set", "jpcp", "--kind", "predictive", "--latency-p99-ms", "300").output)[
            "state"
        ]
        == "unchanged"
    )
    bad = _x("set", "jpcp", "--kind", "predictive", "--pair", "x")
    assert bad.exit_code == 1 and "does not take pair" in bad.output
    assert _x("set", "jpcp", "--kind", "predictive").exit_code == 1
    show = json.loads(_x("show", "jpcp", "--kind", "predictive").output)
    assert show["spec"] == {"latency_p99_ms": 300.0} and show["latest_verdict"] is None
    assert _x("show", "jpcp", "--kind", "agentic").exit_code == 1
    assert [r["servable"] for r in json.loads(_x("list").output)] == ["jpcp"]

    none = _x("check", "jpcp", "--kind", "predictive")
    assert none.exit_code == 1 and json.loads(none.output)["verdict"] == "no_verdict"
    f = tmp_path / "obs.json"
    f.write_text(json.dumps({"latency_ms": [300.0] * 6}))
    ok = _x("check", "jpcp", "--kind", "predictive", "--samples", str(f))
    assert ok.exit_code == 0 and json.loads(ok.output)["verdict"] == "met"
    f.write_text(json.dumps({"latency_ms": [301.0] * 6}))
    viol = _x("check", "jpcp", "--kind", "predictive", "--samples", str(f), "--record")
    assert viol.exit_code == 1 and json.loads(viol.output)["recorded_id"]
    assert (
        json.loads(_x("show", "jpcp", "--kind", "predictive").output)["latest_verdict"]["verdict"]
        == "violated"
    )
    f.write_text("[1]")
    assert _x("check", "jpcp", "--kind", "predictive", "--samples", str(f)).exit_code == 1
    assert (
        _x("check", "jpcp", "--kind", "predictive", "--samples", str(tmp_path / "x")).exit_code == 1
    )


def test_cli_human_output_paths():
    r = runner.invoke(
        app, ["slo", "spec", "set", "m", "--kind", "predictive", "--error-rate", "0.1"]
    )
    assert r.exit_code == 0 and "created" in r.output
    assert runner.invoke(app, ["slo", "spec", "list"]).exit_code == 0
    assert runner.invoke(app, ["slo", "spec", "show", "m", "--kind", "predictive"]).exit_code == 0
    assert runner.invoke(app, ["slo", "spec", "check", "m", "--kind", "predictive"]).exit_code == 1


# ── the promotion gate ──────────────────────────────────────────────────────────────────────

_ALIAS = {"registered_model": {"aliases": [{"alias": "Staging", "version": "19"}]}}
_VER = {"model_version": {"run_id": "run-abc", "version": "19"}}
_RUN = {"run": {"data": {"metrics": {"rmse": 4.5}, "params": {}, "tags": []}}}


def _get(url, **_):
    if "registered-models/get" in url:
        return _ALIAS
    if "model-versions/get" in url:
        return _VER
    if "runs/get" in url:
        return _RUN
    return {}


def _promote(*flags):
    args = ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0", *flags]
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, args)
    return res, post


def test_gate_off_is_byte_identical_no_spec_no_audit_no_read():
    with patch("examlops.slo.specs.gate_verdicts") as spy:
        res, post = _promote()
    assert res.exit_code == 0 and post.called
    spy.assert_not_called()  # never even consulted
    assert not [e for e in export_audit_events() if "slo" in str(e["action"]).lower()]


def test_gate_monitor_records_would_deny_and_promotes():
    _arm("monitor")
    res, post = _promote()
    assert res.exit_code == 0 and post.called
    assert _events("policy_gate_monitor:slo")


def test_gate_enforce_refuses_with_no_spec_and_force_does_not_override():
    _arm("enforce")
    for flags in ((), ("--force",)):
        res, post = _promote(*flags)
        assert res.exit_code == 1, res.output
        assert "no SLOSpec is declared for jpcp" in res.output.replace("\n", " ")
        assert not post.called


def test_gate_enforce_refuses_when_spec_has_no_data_or_is_violated_and_allows_when_met():
    _arm("enforce")
    specs.set_spec("jpcp", "predictive", {"latency_p99_ms": 300})
    res, post = _promote()
    assert res.exit_code == 1 and not post.called  # spec, but no data: no_verdict
    _calls("jpcp", 20, 900.0)
    res, post = _promote("--force")
    assert res.exit_code == 1 and not post.called  # violated; --force cannot override
    assert "violated" in res.output
    _calls("other", 1, 1.0)
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("DELETE FROM gateway_calls WHERE model='jpcp'")
    _calls("jpcp", 20, 300.0)  # exactly at the threshold
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert post.called


def test_recorded_verdict_counts_until_the_spec_changes_or_it_ages_out(tmp_path):
    _arm("enforce")
    specs.set_spec("jpcp", "predictive", {"latency_p99_ms": 300})
    specs.check_spec(
        "jpcp", "predictive", samples={"latency_ms": [10.0] * 6}, record=True, actor="t"
    )
    res, post = _promote()
    assert res.exit_code == 0 and post.called
    # changing the spec bumps the version: the old verdict no longer speaks for it
    specs.set_spec("jpcp", "predictive", {"latency_p99_ms": 200})
    res, post = _promote()
    assert res.exit_code == 1 and not post.called
    specs.check_spec(
        "jpcp", "predictive", samples={"latency_ms": [10.0] * 6}, record=True, actor="t"
    )
    assert _promote()[0].exit_code == 0
    # and a verdict older than the maximum age stops counting
    with patch("examlops.slo.specs.time.time", return_value=time.time() + 169 * 3600):
        assert _promote()[0].exit_code == 1


def test_a_gate_that_cannot_evaluate_denies():
    _arm("enforce")
    with patch("examlops.slo.specs.gate_verdicts", side_effect=RuntimeError("db down")):
        res, post = _promote()
    assert (
        res.exit_code == 1
        and not post.called
        and "could not be evaluated" in " ".join(res.output.split())
    )


def test_policy_list_shows_the_slo_gate_armed():
    _arm("enforce")
    from examlops.policy_engine.gates import configured_gates, gate_mode

    assert "slo" in configured_gates() and gate_mode("slo") == "enforce"


def test_yaml_gates_block_arms_slo(env):
    (env / "policy.yaml").write_text("gates:\n  slo: enforce\n")
    from examlops.policy_engine.gates import gate_mode

    assert gate_mode("slo") == "enforce"


# ── autopilot shares the refusal ────────────────────────────────────────────────────────────


def _cycle():
    set_autopilot_config("enabled", "1")
    set_promotion_rule("JPCP", "rmse", "lt", 5.0, "Staging", "Production")
    with (
        patch.object(autopilot_cmd, "_alias_version", lambda model, alias="Production": "4"),
        patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}),
        patch.object(autopilot_cmd, "_do_promote") as promote,
    ):
        result = autopilot_cmd.run_cycle()
    return result, promote


def test_autopilot_off_and_monitor_promote_enforce_refuses():
    result, promote = _cycle()
    promote.assert_called_once()
    _arm("monitor")
    _, promote = _cycle()
    promote.assert_called_once()
    _arm("enforce")
    result, promote = _cycle()
    promote.assert_not_called()
    assert any("no SLOSpec" in b["reason"] for b in result["policy_blocks"])
    assert any("slo_gate" in str(e["details"]) for e in _events("autopilot_promote_blocked"))


def test_autopilot_enforce_promotes_with_a_met_recorded_spec():
    _arm("enforce")
    specs.set_spec("JPCP", "predictive", {"latency_p99_ms": 300})
    specs.check_spec("JPCP", "predictive", samples={"latency_ms": [1.0] * 6}, record=True)
    _, promote = _cycle()
    promote.assert_called_once()


# ── agent-version promotion ─────────────────────────────────────────────────────────────────


def _agent_doc():
    return {
        "schema_version": 1,
        "agent": "jobdoc",
        "code": {
            "image": "ghcr.io/example/jobdoc@" + DIGEST,
            "entrypoint": "app.graph:build",
            "framework": "langgraph",
        },
        "prompts": [{"name": "jobdoc-system", "version": 7}],
        "models": [
            {"role": "planner", "servable": "gen://q", "binding": "follow", "alias": "production"}
        ],
        "tools": {"tools": [{"name": "docs.search", "schema_hash": DIGEST}], "grants": []},
        "policy": {"contract": "jobdoc-v2", "autonomy": "L2", "multitask_strategy": "enqueue"},
        "eval": {"suites": ["jobdoc-trajectory@2"], "non_inferiority_margin": 0.03},
    }


def _evidenced_version():
    vid = av.register(_agent_doc())["version_id"]
    set_eval_gate(
        "agent-jobdoc", "jobdoc-trajectory", [{"name": "task_success", "min": 0.8}], mode="block"
    )
    record_judge_calibration(_cal("good-judge"))
    record_eval_result(
        "jobdoc-trajectory",
        "agent-jobdoc",
        {"task_success": 0.93},
        run_id="r1",
        model_version=vid,
        sample_size=100,
        judge={"model": "good-judge"},
    )
    return vid


def test_agent_promotion_ignores_slo_when_the_gate_is_off_even_with_a_failing_spec():
    vid = _evidenced_version()
    specs.set_spec("jobdoc", "agentic", {"task_success": 0.9, "judge": "good-judge"})
    out = av.set_alias("jobdoc", "Production", vid)
    assert "slo" not in out["evidence"]


def test_agent_promotion_without_a_spec_is_unaffected_by_an_armed_gate():
    vid = _evidenced_version()
    _arm("enforce")
    out = av.set_alias("jobdoc", "Production", vid)
    assert out["evidence"]["gate_passed"] is True and "slo" not in out["evidence"]


def test_agent_promotion_with_an_agentic_spec_needs_it_met_when_armed():
    vid = _evidenced_version()
    specs.set_spec("jobdoc", "agentic", {"task_success": 0.9, "judge": "good-judge"})
    _arm("enforce")
    with pytest.raises(av.GateRefusal) as exc:
        av.set_alias("jobdoc", "Production", vid)
    assert any("agentic SLO" in r for r in exc.value.reasons)
    assert _events("agent_promotion_blocked")
    with pytest.raises(LookupError):
        av.resolve("jobdoc")  # the alias did not move
    # record a met verdict from judged tasks: now the move succeeds and carries the verdict
    v = specs.check_spec("jobdoc", "agentic", samples=_tasks(), record=True)
    assert v.verdict == "met", v.reasons
    out = av.set_alias("jobdoc", "Production", vid)
    assert out["evidence"]["slo"][0]["verdict"] == "met"
    assert out["evidence"]["slo"][0]["basis"] == "recorded"


def test_agent_promotion_monitor_mode_moves_but_keeps_the_verdict_and_a_would_deny_row():
    vid = _evidenced_version()
    specs.set_spec("jobdoc", "agentic", {"task_success": 0.9, "judge": "good-judge"})
    _arm("monitor")
    out = av.set_alias("jobdoc", "Production", vid)
    assert out["evidence"]["slo"][0]["verdict"] == "no_verdict"
    assert _events("policy_gate_monitor:slo")


def test_staging_moves_never_consult_the_slo_gate():
    vid = av.register(_agent_doc())["version_id"]
    specs.set_spec("jobdoc", "agentic", {"task_success": 0.9, "judge": "good-judge"})
    _arm("enforce")
    assert av.set_alias("jobdoc", "Staging", vid)["alias"] == "Staging"

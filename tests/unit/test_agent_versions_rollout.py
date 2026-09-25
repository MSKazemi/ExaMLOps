"""ADR 0146 decisions 2-6: non-inferiority, state compatibility, session canary, rollback policy,
follow-binding re-evaluation, the evidence pack, and the agent snapshot that carries them.

Real registry, real eval gate and judge-calibration rule, real audit chain, real CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import agent_versions as av  # noqa: E402
from examlops.agent_runtime.routing import starts_on_canary  # noqa: E402
from examlops.agent_runtime.snapshot import (  # noqa: E402
    compile_agent_snapshot,
    load_snapshot_file,
    validate_snapshot,
    write_snapshot,
)
from examlops.agent_versions.compat import (  # noqa: E402
    gate_state,
    state_compat,
    state_schema_hash,
)
from examlops.agent_versions.evidence_pack import export_evidence_pack, pack_digest  # noqa: E402
from examlops.agent_versions.reeval import (  # noqa: E402
    dependents,
    on_model_alias_changed,
    pinned_overrides,
    resolve,
)
from examlops.analysis.ab_stats import (  # noqa: E402
    non_inferiority_proportions,
    non_inferiority_welch,
)
from examlops.cli.main import app  # noqa: E402
from examlops.data import agent_versions as store  # noqa: E402
from examlops.data.audit import dropped_audit_events, export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result  # noqa: E402
from examlops.platform_db import init_db, set_eval_gate  # noqa: E402

runner = CliRunner()
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/jobdoc@" + DIGEST


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    for k in ("EXAMLOPS_SIGNING_KEY", "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("examlops.secrets.get_secret", _no_secret)
    init_db()


def _no_secret(*_a, **_k):
    raise LookupError("no secret store in this test")


SCHEMA = {
    "fields": {"messages": {"type": "list"}, "step": {"type": "int", "default": 0}},
    "nodes": ["plan", "act", "review"],
    "interrupt_nodes": ["review"],
}


def _state(schema):
    return {"schema_version": 1, "schema_hash": state_schema_hash(schema), "schema": schema}


def _doc(prompt=7, *, state=SCHEMA, margin=0.03, reeval=None):
    d = {
        "schema_version": 1,
        "agent": "jobdoc",
        "code": {"image": IMAGE, "entrypoint": "app.graph:build", "framework": "python"},
        "prompts": [{"name": "jobdoc-system", "version": prompt}],
        "models": [
            {
                "role": "planner",
                "servable": "gen://qwen3-32b",
                "binding": "follow",
                "alias": "Production",
            },
        ],
        "tools": {"tools": [], "grants": []},
        "policy": {"autonomy": "L2", "multitask_strategy": "enqueue"},
        "eval": {"suites": ["jobdoc-trajectory@2"], "non_inferiority_margin": margin},
    }
    if state is not None:
        d["state"] = _state(state)
    if reeval:
        d["policy"]["reeval_on_follow"] = reeval
    return d


def _gate():
    set_eval_gate(
        "agent-jobdoc",
        "jobdoc-trajectory",
        [{"name": "task_success", "min": 0.5}],
        mode="block",
        updated_by="test",
    )


def _result(vid, score, n=2000):
    record_eval_result(
        "jobdoc-trajectory",
        "agent-jobdoc",
        {"task_success": score},
        run_id=f"r-{vid[-8:]}-{score}-{n}",
        model_version=vid,
        sample_size=n,
    )


def _promoted(v, score=0.9, n=2000):
    _result(v, score, n)
    return av.set_alias("jobdoc", "Production", v)


# -- statistics -----------------------------------------------------------------------------------


def test_non_inferiority_needs_the_one_sided_bound_inside_the_margin():
    ok = non_inferiority_proportions(900, 1000, 900, 1000, margin=0.05)
    assert ok["non_inferior"] and ok["lower"] > -0.05 and ok["difference"] == 0
    small = non_inferiority_proportions(36, 40, 36, 40, margin=0.05)
    assert not small["non_inferior"]  # same rate, too few samples to rule out a 5-point loss
    worse = non_inferiority_proportions(850, 1000, 900, 1000, margin=0.03)
    assert not worse["non_inferior"] and worse["difference"] == pytest.approx(-0.05)
    # A lower-is-better rate (unsafe answers) is judged on the upper bound.
    safe = non_inferiority_proportions(10, 2000, 10, 2000, margin=0.01, higher_is_better=False)
    assert safe["non_inferior"] and safe["upper"] < 0.01
    with pytest.raises(ValueError):
        non_inferiority_proportions(1, 0, 1, 10, margin=0.1)
    with pytest.raises(ValueError):
        non_inferiority_proportions(1, 10, 1, 10, margin=-1)


def test_continuous_non_inferiority_is_one_sided():
    base = [1.0, 1.1, 0.9, 1.0, 1.05, 0.95] * 10
    assert non_inferiority_welch([x + 0.01 for x in base], base, margin=0.1)["non_inferior"]
    assert not non_inferiority_welch([x - 0.3 for x in base], base, margin=0.1)["non_inferior"]
    # latency: lower is better, a candidate 0.3 slower is inferior
    assert not non_inferiority_welch(
        [x + 0.3 for x in base], base, margin=0.1, higher_is_better=False
    )["non_inferior"]


# -- state compatibility (decision 5) -------------------------------------------------------------


def test_state_compat_outcomes():
    added = {**SCHEMA, "fields": {**SCHEMA["fields"], "notes": {"type": "str", "default": ""}}}
    assert state_compat(_state(SCHEMA), _state(added))["outcome"] == "compatible"
    no_default = {**SCHEMA, "fields": {**SCHEMA["fields"], "notes": {"type": "str"}}}
    assert state_compat(_state(SCHEMA), _state(no_default))["outcome"] == "incompatible"
    renamed = {
        **SCHEMA,
        "fields": {"msgs": {"type": "list"}, "step": {"type": "int", "default": 0}},
    }
    r = state_compat(_state(SCHEMA), _state(renamed))
    assert r["outcome"] == "incompatible" and any("'messages'" in x for x in r["reasons"])
    node_gone = {**SCHEMA, "nodes": ["plan", "act"], "interrupt_nodes": []}
    r = state_compat(_state(SCHEMA), _state(node_gone))
    assert r["outcome"] == "incompatible" and "parked" in r["reasons"][0]
    retyped = {**SCHEMA, "fields": {**SCHEMA["fields"], "step": {"type": "str", "default": "0"}}}
    assert state_compat(_state(SCHEMA), _state(retyped))["outcome"] == "incompatible"
    assert state_compat(None, _state(SCHEMA))["outcome"] == "inert"  # never "compatible"
    inert = state_compat({"schema_version": 1, "schema_hash": DIGEST}, _state(SCHEMA))
    assert inert["outcome"] == "inert"
    assert gate_state(inert, None) and gate_state(inert, "pin") == []
    assert gate_state(inert, "bogus")


def test_a_schema_whose_hash_does_not_match_is_refused_at_registration():
    d = _doc()
    d["state"]["schema_hash"] = DIGEST
    with pytest.raises(av.AgentManifestError) as exc:
        av.register(d)
    assert any("does not match state.schema" in p for p in exc.value.problems)
    bad = _doc(state={"fields": {}, "nodes": []})
    with pytest.raises(av.AgentManifestError):
        av.register(bad)


def test_multitask_strategy_and_reeval_policy_are_validated():
    d = _doc()
    d["policy"]["multitask_strategy"] = "double-text"
    with pytest.raises(av.AgentManifestError):
        av.register(d)
    with pytest.raises(av.AgentManifestError):
        av.register(_doc(reeval="sometimes"))


# -- the extended Production gate (decision 3) ----------------------------------------------------


def test_first_production_needs_no_incumbent_and_records_why():
    _gate()
    v1 = av.register(_doc())["version_id"]
    out = _promoted(v1)
    assert out["ok"] and "non_inferiority" not in out["evidence"]  # nothing to compare against


def test_a_candidate_that_is_not_non_inferior_is_refused_with_the_measured_difference():
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1, 0.92)
    v2 = av.register(_doc(prompt=8))["version_id"]
    _result(v2, 0.86)  # passes the gate's floor (0.5), but 6 points worse
    with pytest.raises(av.GateRefusal) as exc:
        av.set_alias("jobdoc", "Production", v2, state_strategy="pin")
    msg = " ".join(exc.value.reasons)
    assert "not non-inferior on 'task_success'" in msg and "-0.0600" in msg
    assert store.get_alias("jobdoc", "Production")["version_id"] == v1


def test_too_few_samples_cannot_prove_non_inferiority():
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1, 0.9, n=50)
    v2 = av.register(_doc(prompt=8))["version_id"]
    _result(v2, 0.9, n=50)
    with pytest.raises(av.GateRefusal):
        av.set_alias("jobdoc", "Production", v2)


def test_non_inferiority_reads_the_newest_score_even_within_the_same_second():
    # ``ts`` has one-second resolution. The gate used to order by ts alone and take the first
    # row per metric, so a re-evaluation recorded in the same second as the run it replaces
    # could lose to it; the row id now breaks the tie, in SQL, per version.
    from examlops.platform_db import get_db

    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1, 0.90)
    v2 = av.register(_doc(prompt=8))["version_id"]
    _result(v2, 0.70)  # a broken first run...
    _result(v2, 0.91)  # ...re-run and fixed
    with get_db() as conn:
        conn.execute(
            "UPDATE eval_suite_results SET ts='2026-09-25 12:00:00' WHERE model_version=?", (v2,)
        )
        conn.commit()
    out = av.set_alias("jobdoc", "Production", v2, state_strategy="pin")
    ni = out["evidence"]["non_inferiority"]["metrics"][0]
    assert ni["non_inferior"] and ni["rate_candidate"] == pytest.approx(0.91)


def test_a_compatible_non_inferior_candidate_promotes_and_the_evidence_is_recorded():
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1, 0.90)
    added = {**SCHEMA, "fields": {**SCHEMA["fields"], "notes": {"type": "str", "default": ""}}}
    v2 = av.register(_doc(prompt=8, state=added))["version_id"]
    _result(v2, 0.91)
    out = av.set_alias("jobdoc", "Production", v2)
    ni = out["evidence"]["non_inferiority"]
    assert ni["applied"] and ni["metrics"][0]["tested"] and ni["metrics"][0]["non_inferior"]
    assert out["evidence"]["state_compat"]["outcome"] == "compatible"
    hist = json.loads(store.alias_history("jobdoc", "Production")[0]["evidence_json"])
    assert hist["state_compat"]["from"] == v1


def test_an_incompatible_state_blocks_unless_pin_or_drain_is_declared():
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1)
    node_gone = {**SCHEMA, "nodes": ["plan", "act"], "interrupt_nodes": []}
    v2 = av.register(_doc(prompt=8, state=node_gone))["version_id"]
    _result(v2, 0.9)
    with pytest.raises(av.GateRefusal) as exc:
        av.set_alias("jobdoc", "Production", v2)
    assert any("state change is incompatible" in r for r in exc.value.reasons)
    blocked = [e for e in export_audit_events() if e["action"] == "agent_promotion_blocked"]
    assert blocked
    with pytest.raises(ValueError):
        av.set_alias("jobdoc", "Production", v2, state_strategy="migrate")
    out = av.set_alias("jobdoc", "Production", v2, state_strategy="pin")
    assert out["evidence"]["state_compat"]["strategy"] == "pin"


# -- session canary + rollback in-flight policy (decision 4) ---------------------------------------


def test_canary_share_needs_a_canary_version_and_is_audited():
    v1 = av.register(_doc())["version_id"]
    with pytest.raises(LookupError):
        av.set_canary("jobdoc", 10)
    av.set_alias("jobdoc", "Canary", v1)
    with pytest.raises(ValueError):
        av.set_canary("jobdoc", 101)
    out = av.set_canary("jobdoc", 10, reason="try")
    assert out["canary_percent"] == 10 and out["previous"] is None
    assert [e for e in export_audit_events() if e["action"] == "agent_canary_set"]


def test_ten_percent_canary_hits_ten_percent_of_new_sessions_only():
    keys = [f"th-{i:05d}" for i in range(4000)]
    share = sum(starts_on_canary(k, 10) for k in keys) / len(keys)
    assert 0.085 < share < 0.115  # 10% +- ~3 standard errors at n=4000
    assert all(not starts_on_canary(k, 0) for k in keys)
    # Raising the share never moves a session that was on Canary off it.
    assert all(starts_on_canary(k, 30) for k in keys if starts_on_canary(k, 10))


def test_rollback_records_the_in_flight_policy_and_the_snapshot_carries_it():
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1)
    v2 = av.register(_doc(prompt=8))["version_id"]
    _result(v2, 0.9)
    av.set_alias("jobdoc", "Production", v2, state_strategy="drain")
    with pytest.raises(ValueError):
        av.rollback("jobdoc", "Production", in_flight="explode")
    out = av.rollback("jobdoc", "Production", in_flight="quarantine")
    assert out["in_flight"] == "quarantine" and out["version_id"] == v1
    snap = compile_agent_snapshot(models={"qwen3-32b": {"Production": "7"}})
    entry = snap["agents"]["jobdoc"]
    assert entry["aliases"]["Production"] == v1
    assert entry["retired"] == {v2: "quarantine"}
    assert v2 in snap["versions"] and validate_snapshot(snap) == []


def test_the_snapshot_carries_state_migrations_canary_grants_and_quotas(tmp_path):
    from examlops import tool_broker as tb

    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1)
    added = {**SCHEMA, "fields": {**SCHEMA["fields"], "notes": {"type": "str", "default": ""}}}
    v2 = av.register(_doc(prompt=8, state=added))["version_id"]
    _result(v2, 0.9)
    av.set_alias("jobdoc", "Production", v2)
    av.set_alias("jobdoc", "Canary", v1)
    av.set_canary("jobdoc", 5)
    tb.set_grant(v2, "authz_relations", {"effect": "allow"})
    snap = compile_agent_snapshot(quotas={"default": {"max_sessions": 3}, "tenants": {}})
    assert snap["agents"]["jobdoc"]["migrations"] == {v1: {"to": v2, "outcome": "compatible"}}
    assert snap["agents"]["jobdoc"]["canary_percent"] == 5
    assert snap["grants"][v2]["authz_relations"]["effect"] == "allow"
    assert snap["quotas"]["default"]["max_sessions"] == 3
    path = write_snapshot(snap, tmp_path / "snap" / "agent.json")
    assert load_snapshot_file(path)["digest"] == snap["digest"]
    doc = json.loads(path.read_text())
    doc["agents"]["jobdoc"]["canary_percent"] = 100  # edited on disk
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="digest"):
        load_snapshot_file(path)


# -- follow-binding re-evaluation (decision 2, verification 6) --------------------------------------


def test_a_followed_model_promotion_enqueues_reevaluation_for_every_dependent():
    from examlops import events

    v1 = av.register(_doc())["version_id"]
    v2 = av.register(_doc(prompt=8, reeval="blocking"))["version_id"]
    pinned = _doc(prompt=9)
    pinned["models"] = [
        {"role": "planner", "servable": "gen://qwen3-32b", "binding": "pin", "version": 7}
    ]
    av.register(pinned)
    assert {d["row"]["version_id"] for d in dependents("QWEN3-32B", "production")} == {v1, v2}

    events.alias_changed("qwen3-32b", "Production", 8, previous_version=7, actor="ci")
    queue = store.list_reevals(status="pending")
    assert {q["version_id"] for q in queue} == {v1, v2}
    assert {q["blocking"] for q in queue if q["version_id"] == v2} == {1}
    enq = [e for e in export_audit_events() if e["action"] == "agent_reeval_enqueued"]
    assert len(enq) == 2  # on the evidence chain
    # Announced twice: not enqueued twice. Same version: nothing to do.
    events.alias_changed("qwen3-32b", "Production", 8, previous_version=7)
    assert on_model_alias_changed("qwen3-32b", "Production", 8, previous_version=8) == []
    assert len(store.list_reevals()) == 2

    # The blocking agent is held on the model version it was evaluated with ...
    assert pinned_overrides() == {v2: {"qwen3-32b@Production": "7"}}
    entry = next(q for q in queue if q["version_id"] == v2)
    resolve(entry["id"], "failed", reason="regressed")
    assert pinned_overrides() == {v2: {"qwen3-32b@Production": "7"}}  # failed keeps the pin
    with pytest.raises(LookupError):
        resolve(entry["id"], "passed")  # the first verdict stands


def test_passing_reevaluation_releases_the_pin():
    v2 = av.register(_doc(prompt=8, reeval="blocking"))["version_id"]
    created = on_model_alias_changed("qwen3-32b", "Production", "8", previous_version="7")
    assert pinned_overrides() == {v2: {"qwen3-32b@Production": "7"}}
    with pytest.raises(ValueError):
        resolve(created[0]["id"], "maybe")
    resolve(created[0]["id"], "passed")
    assert pinned_overrides() == {}


# -- the evidence pack (decision 6) -----------------------------------------------------------------


def test_the_evidence_pack_holds_tuple_evaluations_moves_and_audit_and_is_tamper_evident():
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1)
    v2 = av.register(_doc(prompt=8))["version_id"]
    _result(v2, 0.9)
    av.set_alias("jobdoc", "Production", v2, state_strategy="pin")
    av.rollback("jobdoc", "Production", in_flight="interrupt")
    pack = export_evidence_pack(v2)
    assert (
        pack["version"]["version_id"] == v2
        and pack["version"]["manifest"]["prompts"][0]["version"] == 8
    )
    res = pack["evaluation"]["results"]
    assert res and res[0]["score_lo"] is not None and res[0]["sample_size"] == 2000
    assert pack["evaluation"]["gate_reports"]  # the gate ran for v2
    actions = [m["action"] for m in pack["promotions_and_rollbacks"]]
    assert actions == ["set", "rollback"]
    assert pack["promotions_and_rollbacks"][0]["evidence"]["state_compat"]["strategy"] == "pin"
    audited = {e["action"] for e in pack["audit"]["events"]}
    assert {"agent_version_registered", "agent_alias_moved", "agent_alias_rolled_back"} <= audited
    assert pack["digest"] == pack_digest(pack)
    pack["evaluation"]["results"][0]["score"] = 1.0
    assert pack["digest"] != pack_digest(pack)
    # Another agent's events never leak into this pack.
    assert all(e["target"].startswith("jobdoc") for e in pack["audit"]["events"])
    with pytest.raises(LookupError):
        export_evidence_pack("av-sha256:" + "0" * 64)


# -- CLI ---------------------------------------------------------------------------------------------


def _run(*args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def test_cli_evidence_compat_canary_and_reeval(tmp_path):
    _gate()
    v1 = av.register(_doc())["version_id"]
    _promoted(v1)
    node_gone = {**SCHEMA, "nodes": ["plan", "act"], "interrupt_nodes": []}
    v2 = av.register(_doc(prompt=8, state=node_gone))["version_id"]

    r = _run("--json", "agent", "version", "compat", v1, v2)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["outcome"] == "incompatible"

    out = tmp_path / "pack.json"
    r = _run("--json", "agent", "version", "evidence", v1, "--out", str(out))
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text())["digest"] == json.loads(r.output)["digest"]

    r = _run("--json", "agent", "alias", "canary", "jobdoc", "10")
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "not_found"  # no Canary yet
    av.set_alias("jobdoc", "Canary", v2)
    r = _run("--json", "agent", "alias", "canary", "jobdoc", "10")
    assert r.exit_code == 0 and json.loads(r.output)["canary_percent"] == 10

    on_model_alias_changed("qwen3-32b", "Production", "8", previous_version="7")
    r = _run("--json", "agent", "version", "reeval", "--status", "pending")
    rows = json.loads(r.output)
    assert r.exit_code == 0 and {x["version_id"] for x in rows} == {v1, v2}
    r = _run(
        "--json", "agent", "version", "reeval-resolve", str(rows[0]["id"]), "--outcome", "passed"
    )
    assert r.exit_code == 0 and json.loads(r.output)["status"] == "passed"
    r = _run(
        "--json", "agent", "version", "reeval-resolve", str(rows[0]["id"]), "--outcome", "passed"
    )
    assert r.exit_code == 1

    r = _run("--json", "agent", "alias", "set", "jobdoc", "Production", v2)
    assert r.exit_code == 1  # no results for v2 and an incompatible schema
    r = _run(
        "--json", "agent", "alias", "rollback", "jobdoc", "Canary", "--in-flight", "quarantine"
    )
    assert r.exit_code == 1  # Canary has no earlier version


def test_cli_runtime_snapshot_writes_a_valid_document(tmp_path):
    v1 = av.register(_doc())["version_id"]
    av.set_alias("jobdoc", "Staging", v1)
    out = tmp_path / "agent-snapshot.json"
    r = _run("--json", "agent", "runtime", "snapshot", "--out", str(out))
    assert r.exit_code == 0, r.output
    doc = load_snapshot_file(out)
    assert doc["agents"]["jobdoc"]["aliases"] == {"Staging": v1}
    assert json.loads(r.output)["digest"] == doc["digest"]


# -- a lost audit on the new mutations is counted ----------------------------------------------------


def _break_audit(monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import reset_dropped_audit_events

    reset_dropped_audit_events()

    def boom(*_a, **_k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_canary_audit_is_counted(monkeypatch):
    v1 = av.register(_doc())["version_id"]
    av.set_alias("jobdoc", "Canary", v1)
    _break_audit(monkeypatch)
    assert av.set_canary("jobdoc", 5)["ok"]
    assert dropped_audit_events().get("agent_canary_set") == 1


def test_a_lost_reeval_audit_is_counted(monkeypatch):
    av.register(_doc(reeval="blocking"))
    _break_audit(monkeypatch)
    created = on_model_alias_changed("qwen3-32b", "Production", "8", previous_version="7")
    assert created and dropped_audit_events().get("agent_reeval_enqueued") == 1
    resolve(created[0]["id"], "passed")
    assert dropped_audit_events().get("agent_reeval_resolved") == 1

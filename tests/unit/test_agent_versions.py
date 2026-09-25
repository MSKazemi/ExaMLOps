"""Agent versions as registry artifacts (ADR 0146).

Real code paths end to end: the real manifest validator, a real sqlite ``platform.db`` (the
suite's per-test one), the real eval gate and judge-calibration rule (ADR 0111), the real audit
chain and the real CLI. Nothing here mocks the registry or the gate.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import agent_versions as av  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import agent_versions as store  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result, record_judge_calibration  # noqa: E402
from examlops.evaluation import calibration as cal_mod  # noqa: E402
from examlops.platform_db import init_db, set_eval_gate  # noqa: E402

runner = CliRunner()
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/jobdoc@" + DIGEST


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    for k in (
        "EXAMLOPS_SIGNING_KEY",
        "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE",
        "EXAMLOPS_AGENT_VERSION_PIN",
    ):
        monkeypatch.delenv(k, raising=False)
    # No secret store in unit tests: a real signing lookup would try to reach one.
    monkeypatch.setattr("examlops.secrets.get_secret", _no_secret)
    init_db()


def _no_secret(*_a, **_k):
    raise LookupError("no secret store in this test")


def _doc(**over):
    d = {
        "schema_version": 1,
        "agent": "jobdoc",
        "code": {"image": IMAGE, "entrypoint": "app.graph:build", "framework": "langgraph"},
        "prompts": [{"name": "jobdoc-system", "version": 7}],
        "models": [
            {
                "role": "planner",
                "servable": "gen://qwen3-32b",
                "binding": "follow",
                "alias": "production",
            },
            {"role": "embedder", "servable": "gen://embed-e5", "binding": "pin", "version": 4},
        ],
        "tools": {
            "tools": [{"name": "docs.search", "schema_hash": DIGEST}],
            "grants": ["docs.search", "hpc.jobs.read"],
        },
        "policy": {"contract": "jobdoc-v2", "autonomy": "L2", "multitask_strategy": "enqueue"},
        "eval": {"suites": ["jobdoc-trajectory@2"], "non_inferiority_margin": 0.03},
    }
    d.update(over)
    return d


def _events(prefix: str):
    return [e for e in export_audit_events() if str(e["action"]).startswith(prefix)]


# -- manifest: canonicalisation and hash stability (verification 1) ---------------------------


def test_version_id_is_stable_under_key_order_and_normalisation():
    a = av.normalize(_doc())
    reordered = json.loads(json.dumps(_doc(), sort_keys=True))
    reordered["models"] = list(reversed(reordered["models"]))
    b = av.normalize(reordered)
    assert av.version_id_of(a) == av.version_id_of(b)
    assert av.normalize(a) == a  # idempotent: a stored manifest re-validates to itself
    assert av.canonical_json(a) == av.canonical_json(b)
    assert av.version_id_of(a).startswith("av-sha256:")
    assert {m["role"]: m.get("alias") for m in a["models"]}[
        "planner"
    ] == "Production"  # follow alias spelled the registry's way


def test_changing_only_a_prompt_version_makes_a_new_version_id():
    base = av.version_id_of(av.normalize(_doc()))
    changed = _doc(prompts=[{"name": "jobdoc-system", "version": 8}])
    assert av.version_id_of(av.normalize(changed)) != base


def test_a_tool_schema_or_grant_change_is_a_new_version():
    base = av.version_id_of(av.normalize(_doc()))
    t = _doc()
    t["tools"]["tools"][0]["schema_hash"] = "sha256:" + "b" * 64
    g = _doc()
    g["tools"]["grants"].append("jobs.cancel")
    assert av.version_id_of(av.normalize(t)) != base
    assert av.version_id_of(av.normalize(g)) != base


def test_mcp_tool_manifest_pins_real_registry_tools_by_schema_hash():
    pins = av.mcp_tool_manifest(["platform_status", "list_models"])
    assert [p["name"] for p in pins] == ["list_models", "platform_status"]
    assert all(p["schema_hash"].startswith("sha256:") for p in pins)
    assert pins == av.mcp_tool_manifest(["list_models", "platform_status"])
    with pytest.raises(LookupError):
        av.mcp_tool_manifest(["no_such_tool"])


# -- manifest: refusals ----------------------------------------------------------------------


def _problems(doc) -> list[str]:
    with pytest.raises(av.AgentManifestError) as exc:
        av.normalize(doc)
    return exc.value.problems


def test_floating_and_unpinned_references_are_refused_by_name():
    d = _doc(prompts=[{"name": "jobdoc-system", "label": "prod"}])
    p = _problems(d)
    assert any("prompts[0].label" in x and "floating" in x for x in p)
    assert any("prompts[0].version" in x for x in p)  # and the missing pin is named too

    d = _doc()
    d["code"]["image"] = "ghcr.io/example/jobdoc:latest"
    assert any("pinned by digest" in x for x in _problems(d))

    d = _doc()
    d["models"][1].pop("version")
    assert any("pin binding requires a version" in x for x in _problems(d))

    d = _doc()
    d["models"][0]["version"] = 3
    assert any("follow binding may not carry a version" in x for x in _problems(d))

    d = _doc(eval={"suites": ["jobdoc-trajectory"]})
    assert any("eval.suites[0]" in x for x in _problems(d))

    d = _doc()
    d["tools"]["tools"][0]["schema_hash"] = "latest"
    assert any("schema_hash" in x for x in _problems(d))


def test_missing_components_unknown_fields_and_bad_values_are_all_listed():
    d = _doc()
    for k in ("code", "prompts", "tools"):
        d.pop(k)
    d["surprise"] = 1
    d["policy"]["autonomy"] = "L9"
    p = _problems(d)
    assert any(x.startswith("code:") and "missing" in x for x in p)
    assert any(x.startswith("prompts:") for x in p)
    assert any(x.startswith("tools:") for x in p)
    assert any("surprise" in x and "unknown" in x for x in p)
    assert any("policy.autonomy" in x for x in p)
    assert _problems([1, 2]) == ["manifest: must be a JSON object"]
    assert any("schema_version" in x for x in _problems(_doc(schema_version=2)))
    assert any("finite" in x for x in _problems(_doc(budgets={"max_cost_usd": float("nan")})))


def test_stale_version_id_or_tool_manifest_hash_is_refused_not_corrected():
    good = av.normalize(_doc())
    stale = copy.deepcopy(good)
    stale["version_id"] = "av-sha256:" + "0" * 64
    assert any("version_id" in x for x in _problems(stale))
    stale = copy.deepcopy(good)
    stale["tools"]["manifest_hash"] = DIGEST
    assert any("manifest_hash" in x for x in _problems(stale))
    ok = copy.deepcopy(good)
    ok["version_id"] = av.version_id_of(good)
    assert av.normalize(ok) == good


# -- registry: idempotence and immutability (verification 1) ---------------------------------


def test_register_is_idempotent_and_a_stored_version_never_changes():
    first = av.register(_doc())
    again = av.register(json.loads(json.dumps(_doc())))
    assert first["created"] is True and again["created"] is False
    assert first["version_id"] == again["version_id"]
    assert len(av.list_versions("jobdoc")) == 1
    assert len(_events("agent_version_registered")) == 1  # the no-op did not audit again

    before = store.get_version(first["version_id"])
    # Even asked to store different bytes under the same id, the store hands back the original.
    created, row = store.insert_version(
        first["version_id"], "jobdoc", '{"tampered":true}', actor="attacker"
    )
    assert created is False and row["manifest_json"] == before["manifest_json"]
    assert store.get_version(first["version_id"])["manifest_json"] == before["manifest_json"]

    other = av.register(_doc(prompts=[{"name": "jobdoc-system", "version": 8}]))
    assert other["version_id"] != first["version_id"]
    assert store.get_version(first["version_id"])["manifest_json"] == before["manifest_json"]


def test_invalid_manifest_is_never_stored():
    with pytest.raises(av.AgentManifestError):
        av.register(_doc(prompts=[]))
    assert av.list_versions() == []


# -- diff (pure) -----------------------------------------------------------------------------


def test_diff_names_the_components_that_changed():
    a = av.normalize(_doc())
    b_doc = _doc(prompts=[{"name": "jobdoc-system", "version": 8}])
    b_doc["tools"]["tools"].append({"name": "hpc.jobs", "schema_hash": DIGEST})
    b_doc["models"] = [m for m in b_doc["models"] if m["role"] != "embedder"]
    b_doc["policy"]["autonomy"] = "L3"
    d = av.diff_manifests(a, av.normalize(b_doc))
    got = {c["component"]: c["change"] for c in d["changes"]}
    assert got == {
        "prompts:jobdoc-system": "changed",
        "tools:hpc.jobs": "added",
        "models:embedder": "removed",
        "policy.autonomy": "changed",
    }
    assert d["identical"] is False
    same = av.diff_manifests(a, av.normalize(_doc()))
    assert same["identical"] is True and same["changes"] == []


# -- aliases, audit, rollback (verification 4 analogue: alias move) --------------------------


def _two_versions():
    v1 = av.register(_doc())["version_id"]
    v2 = av.register(_doc(prompts=[{"name": "jobdoc-system", "version": 8}]))["version_id"]
    return v1, v2


def test_staging_and_canary_move_freely_and_every_move_is_audited():
    v1, v2 = _two_versions()
    out = av.set_alias("jobdoc", "staging", v1)
    assert out["alias"] == "Staging" and out["previous"] is None
    av.set_alias("jobdoc", "Staging", v2, reason="try v2")
    assert av.resolve("jobdoc", "Staging").version_id == v2
    ev = _events("agent_alias_moved")
    assert len(ev) == 2 and ev[-1]["target"] == "jobdoc@Staging"
    assert [r["alias"] for r in store.list_aliases("jobdoc")] == ["Staging"]
    assert [v["aliases"] for v in av.list_versions("jobdoc") if v["version_id"] == v2] == [
        ["Staging"]
    ]


def test_alias_refusals_unknown_wrong_agent_bad_alias():
    v1, _ = _two_versions()
    with pytest.raises(ValueError):
        av.set_alias("jobdoc", "Live", v1)
    with pytest.raises(LookupError):
        av.set_alias("jobdoc", "Staging", "av-sha256:" + "0" * 64)
    with pytest.raises(LookupError):
        av.set_alias("someone-else", "Staging", v1)
    with pytest.raises(LookupError):
        av.resolve("jobdoc", "Production")


def test_rollback_restores_the_prior_version_and_is_audited():
    v1, v2 = _two_versions()
    av.set_alias("jobdoc", "Staging", v1)
    av.set_alias("jobdoc", "Staging", v2)
    out = av.rollback("jobdoc", "Staging", reason="regression")
    assert out["version_id"] == v1 and out["previous"] == v2
    assert av.resolve("jobdoc", "Staging").version_id == v1
    assert len(_events("agent_alias_rolled_back")) == 1
    hist = store.alias_history("jobdoc", "Staging")
    assert [h["action"] for h in hist] == ["rollback", "set", "set"]
    with pytest.raises(LookupError):
        av.rollback("jobdoc", "Canary")  # never set: nothing to roll back to
    av.set_alias("jobdoc", "Canary", v1)
    with pytest.raises(LookupError):
        av.rollback("jobdoc", "Canary")  # first move had no predecessor


# -- the promotion gate (verification 2) -----------------------------------------------------


def _gate(mode="block", suite="jobdoc-trajectory", metric="task_success"):
    set_eval_gate(
        "agent-jobdoc", suite, [{"name": metric, "min": 0.8}], mode=mode, updated_by="test"
    )


def _result(vid, *, judge=None, score=0.9, suite="jobdoc-trajectory", metric="task_success"):
    record_eval_result(
        suite,
        "agent-jobdoc",
        {metric: score},
        run_id=f"run-{vid[-6:]}-{score}",
        model_version=vid,
        sample_size=2000,  # enough to show non-inferiority within 0.03 (ADR 0146 d3)
        judge={"model": judge} if judge else None,
    )


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


def _refused(vid) -> list[str]:
    with pytest.raises(av.GateRefusal) as exc:
        av.set_alias("jobdoc", "Production", vid)
    return exc.value.reasons


def test_production_is_refused_when_there_is_no_evaluation_evidence():
    v1 = av.register(_doc())["version_id"]
    assert any("no evaluation evidence" in r for r in _refused(v1))  # no gate at all
    _gate()
    assert any("no evaluation evidence" in r and "no results" in r for r in _refused(v1))
    assert store.get_alias("jobdoc", "Production") is None  # nothing moved
    blocked = _events("agent_promotion_blocked")
    assert len(blocked) == 2 and "no evaluation evidence" in json.dumps(blocked[-1]["details"])


def test_a_warn_mode_gate_cannot_promote_and_evidence_for_another_version_does_not_count():
    v1, v2 = _two_versions()
    _gate(mode="warn")
    _result(v1)
    assert any("warn mode" in r for r in _refused(v1))
    _gate(mode="block")
    assert any("no results" in r for r in _refused(v2))  # v1's score is not v2's evidence


def test_a_failing_score_refuses_and_names_the_metric():
    v1 = av.register(_doc())["version_id"]
    _gate()
    _result(v1, score=0.5)
    assert any("task_success" in r for r in _refused(v1))


def test_an_uncalibrated_judge_blocks_production_and_is_named():
    v1 = av.register(_doc())["version_id"]
    _gate()
    _result(v1, judge="ghost-judge")
    reasons = _refused(v1)
    assert any("ghost-judge" in r and "no_calibration" in r for r in reasons)
    record_judge_calibration(_cal("ghost-judge", position_bias=0.19))
    assert any("position_bias" in r for r in _refused(v1))


def test_production_moves_with_a_calibrated_judge_and_records_the_evidence():
    v1 = av.register(_doc())["version_id"]
    _gate()
    record_judge_calibration(_cal("good-judge"))
    _result(v1, judge="good-judge", score=0.93)
    out = av.set_alias("jobdoc", "Production", v1, reason="evals green")
    assert out["evidence"]["gate_passed"] is True
    assert out["evidence"]["calibration_id"]
    assert av.resolve("jobdoc").version_id == v1
    moved = _events("agent_alias_moved")[-1]
    assert moved["target"] == "jobdoc@Production"
    assert "calibration_id" in json.dumps(moved["details"])
    assert store.alias_history("jobdoc", "Production")[0]["evidence_json"]


def test_a_declared_suite_without_results_blocks_even_when_the_gate_suite_passes():
    v1 = av.register(_doc(eval={"suites": ["jobdoc-trajectory@2", "safety@5"]}))["version_id"]
    _gate()
    _result(v1)
    assert any("'safety'" in r and "no results" in r for r in _refused(v1))
    _result(v1, suite="safety", metric="unsafe_rate", score=0.01)
    assert av.set_alias("jobdoc", "Production", v1)["ok"]


def test_gate_suite_must_be_one_of_the_declared_suites():
    v1 = av.register(_doc())["version_id"]
    _gate(suite="something-else")
    _result(v1, suite="something-else")
    assert any("not among the declared suites" in r for r in _refused(v1))


def test_rollback_of_production_is_not_regated():
    v1, v2 = _two_versions()
    _gate()
    _result(v1)
    _result(v2)
    av.set_alias("jobdoc", "Production", v1)
    av.set_alias("jobdoc", "Production", v2, state_strategy="pin")  # no exported schema: inert (d5)
    # Withdraw the evidence: a rollback is a lookup and must still work.
    set_eval_gate("agent-jobdoc", "jobdoc-trajectory", [{"name": "task_success", "min": 2.0}])
    assert av.rollback("jobdoc", "Production")["version_id"] == v1


# -- supply chain: a signed manifest ---------------------------------------------------------


def test_unsigned_site_is_not_blocked_and_a_configured_site_requires_a_valid_signature(
    monkeypatch,
):
    v1 = av.register(_doc())["version_id"]  # registered while no signing key exists
    _gate()
    _result(v1)
    assert av.set_alias("jobdoc", "Production", v1)["ok"]  # nothing configured: not blocked

    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)
    v2 = av.register(_doc(prompts=[{"name": "jobdoc-system", "version": 8}]))["version_id"]
    _result(v2)
    row = store.get_version(v2)
    assert row["signature"] and row["sign_algo"] == "hmac-sha256"
    assert av.verify_signature(row) is True
    assert av.set_alias("jobdoc", "Production", v2, state_strategy="pin")["ok"]

    # v1 was registered before signing was configured: refused, and told how to fix it.
    assert any("unsigned" in r for r in _refused(v1))

    # A stored signature that does not verify (key rotated / row tampered) is refused too.
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "z" * 32)
    assert av.verify_signature(store.get_version(v2)) is False
    assert any("does not verify" in r for r in _refused(v2))


def test_ed25519_signature_round_trip(monkeypatch, tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    pem = Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    key = tmp_path / "k.pem"
    key.write_bytes(pem)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(key))
    vid = av.register(_doc())["version_id"]
    row = store.get_version(vid)
    assert row["sign_algo"] == "ed25519-v2" and row["sign_key_id"]
    assert av.verify_signature(row) is True
    assert av.verify_signature({**row, "version_id": "av-sha256:" + "1" * 64}) is False


# -- CLI (exit codes, --json) ----------------------------------------------------------------


def _write(tmp_path, doc, name="agent.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def _run(*args, **kw):
    return runner.invoke(app, list(args), **kw)


def test_cli_register_show_list_diff_json(tmp_path):
    f = _write(tmp_path, _doc())
    r = _run("--json", "agent", "version", "register", f)
    assert r.exit_code == 0, r.output
    vid = json.loads(r.output)["version_id"]
    assert json.loads(_run("--json", "agent", "version", "register", f).output)["created"] is False

    shown = json.loads(_run("--json", "agent", "version", "show", vid).output)
    assert shown["manifest"]["agent"] == "jobdoc" and shown["signed"] is False
    listed = json.loads(_run("--json", "agent", "version", "list", "--agent", "jobdoc").output)
    assert [v["version_id"] for v in listed] == [vid]

    f2 = _write(tmp_path, _doc(prompts=[{"name": "jobdoc-system", "version": 9}]), "b.json")
    vid2 = json.loads(_run("--json", "agent", "version", "register", f2).output)["version_id"]
    d = json.loads(_run("--json", "agent", "version", "diff", vid, vid2).output)
    assert [c["component"] for c in d["changes"]] == ["prompts:jobdoc-system"]
    assert _run("agent", "version", "diff", vid, vid).exit_code == 0

    r = _run("--json", "agent", "version", "show", "av-sha256:" + "0" * 64)
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "not_found"
    r = _run("--json", "agent", "version", "diff", vid, "nope")
    assert r.exit_code == 1


def test_cli_register_refuses_an_invalid_manifest_with_every_problem(tmp_path):
    bad = _doc(prompts=[{"name": "p", "label": "prod"}])
    bad["code"]["image"] = "img:latest"
    r = _run("--json", "agent", "version", "register", _write(tmp_path, bad))
    assert r.exit_code == 1
    out = json.loads(r.output)
    assert out["code"] == "invalid_manifest" and len(out["problems"]) >= 3
    assert av.list_versions() == []
    assert _run("agent", "version", "register", str(tmp_path / "missing.json")).exit_code == 1


def test_cli_alias_set_gate_show_and_rollback(tmp_path):
    v1, v2 = _two_versions()
    r = _run("--json", "agent", "alias", "set", "jobdoc", "Production", v1)
    assert r.exit_code == 1
    out = json.loads(r.output)
    assert out["code"] == "promotion_refused" and out["reasons"]
    assert _run("--json", "agent", "alias", "show", "jobdoc").exit_code == 1  # nothing set yet

    assert _run("--json", "agent", "alias", "set", "jobdoc", "Staging", v1).exit_code == 0
    assert _run("--json", "agent", "alias", "set", "jobdoc", "Staging", v2).exit_code == 0
    shown = json.loads(_run("--json", "agent", "alias", "show", "jobdoc", "staging").output)
    assert shown["aliases"][0]["version_id"] == v2 and len(shown["history"]) == 2
    rb = json.loads(_run("--json", "agent", "alias", "rollback", "jobdoc", "Staging").output)
    assert rb["version_id"] == v1
    assert _run("agent", "alias", "rollback", "jobdoc", "Canary").exit_code == 1
    assert _run("agent", "alias", "set", "jobdoc", "Live", v1).exit_code == 1

    ref = json.loads(_run("--json", "agent", "version", "show", "jobdoc@Staging").output)
    assert ref["version_id"] == v1


def test_cli_production_with_evidence_and_a_policy_rule(tmp_path, monkeypatch):
    v1 = av.register(_doc())["version_id"]
    _gate()
    _result(v1)
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "policies:\n  - name: freeze-prod\n    action: agent_promote\n"
        "    when: \"to_alias == 'Production'\"\n    effect: deny\n"
    )
    monkeypatch.setattr("examlops.policy.POLICY_YAML", policy)
    r = _run("agent", "alias", "set", "jobdoc", "Production", v1)
    assert r.exit_code == 1 and "freeze-prod" in r.output
    assert store.get_alias("jobdoc", "Production") is None
    policy.unlink()  # no policy: default unchanged, evidence gate alone decides
    r = _run("--json", "agent", "alias", "set", "jobdoc", "Production", v1)
    assert r.exit_code == 0, r.output


# -- Skipper opt-in consumption (system prompt only) -----------------------------------------


def test_skipper_system_prompt_can_be_pinned_by_an_agent_version(monkeypatch):
    agent_dir = Path(__file__).parents[2] / "platform" / "services" / "agent"
    monkeypatch.syspath_prepend(str(agent_dir))
    from skipper import prompts

    from examlops.data.prompts import create_prompt_version, set_prompt_label

    v_old = create_prompt_version("skipper-system", "OLD PROMPT", variables=[], tags={})
    v_new = create_prompt_version("skipper-system", "NEW PROMPT", variables=[], tags={})
    set_prompt_label("skipper-system", "prod", v_new)
    from examlops import prompts as reg

    reg.clear_cache()
    assert prompts.system_prompt() == "NEW PROMPT"  # unset: behaviour unchanged

    doc = _doc(prompts=[{"name": "skipper-system", "version": v_old}], agent="skipper")
    vid = av.register(doc)["version_id"]
    av.set_alias("skipper", "Staging", vid)
    monkeypatch.setenv("EXAMLOPS_AGENT_VERSION_PIN", "skipper@Staging")
    assert prompts.system_prompt() == "OLD PROMPT"  # pinned by number, not by the moving label

    monkeypatch.setenv("EXAMLOPS_AGENT_VERSION_PIN", "skipper@Canary")  # unresolvable pin
    assert prompts.system_prompt() == "NEW PROMPT"  # fails safe to the label, never crashes
    monkeypatch.setenv("EXAMLOPS_AGENT_VERSION_PIN", "ghost")
    assert prompts.system_prompt() == "NEW PROMPT"


# -- a lost audit event is counted, and never changes the operation --------------------------


def _break_audit(monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import reset_dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit down")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_register_audit_is_counted(monkeypatch):
    from examlops.data.audit import dropped_audit_events

    _break_audit(monkeypatch)
    assert av.register(_doc())["created"] is True  # the operation still succeeds
    assert dropped_audit_events().get("agent_version_registered") == 1


def test_a_lost_alias_move_audit_is_counted(monkeypatch):
    from examlops.data.audit import dropped_audit_events

    v1, _ = _two_versions()
    _break_audit(monkeypatch)
    av.set_alias("jobdoc", "Staging", v1)
    assert av.resolve("jobdoc", "Staging").version_id == v1
    assert dropped_audit_events().get("agent_alias_moved") == 1
    _gate()
    with pytest.raises(av.GateRefusal):  # a lost refusal audit must not turn a refusal into a pass
        av.set_alias("jobdoc", "Production", v1)
    assert dropped_audit_events().get("agent_promotion_blocked") == 1


def test_a_lost_rollback_audit_is_counted(monkeypatch):
    from examlops.data.audit import dropped_audit_events

    v1, v2 = _two_versions()
    av.set_alias("jobdoc", "Staging", v1)
    av.set_alias("jobdoc", "Staging", v2)
    _break_audit(monkeypatch)
    assert av.rollback("jobdoc", "Staging")["version_id"] == v1
    assert dropped_audit_events().get("agent_alias_rolled_back") == 1

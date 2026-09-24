"""GenAI applications as composed, versioned registry artifacts (ADR 0159, Phases 1-2).

Real code paths end to end: the real manifest validator, a real sqlite ``platform.db`` (the
suite's per-test one), the real shared eval gate and judge-calibration rule (ADR 0111), the real
prompt registry, the real audit chain and the real CLI. Nothing here mocks the registry or the
gate.

The one thing these tests *do* control is reachability — a component check that cannot be made to
fail is a check nobody has ever seen work, and "the guardrail could not be reached" is precisely
the case ADR 0026 spent an amendment closing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import genai_apps as ga  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import genai_apps as store  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result, record_judge_calibration  # noqa: E402
from examlops.data.prompts import create_prompt_version, set_prompt_label  # noqa: E402
from examlops.evaluation import calibration as cal_mod  # noqa: E402
from examlops.platform_db import get_db, init_db, set_eval_gate  # noqa: E402

runner = CliRunner()

APP = "hpc-docs-assistant"
KEY = f"genai-app-{APP}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    # A site gateway.yaml would make the route check read a file this suite does not own.
    monkeypatch.delenv("EXAMLOPS_GATEWAY_CONFIG", raising=False)
    monkeypatch.delenv("EXAMLOPS_LLM_OLLAMA_URL", raising=False)
    monkeypatch.delenv("EXAMLOPS_LLM_GATEWAY_URL", raising=False)
    monkeypatch.setattr("examlops.gateway.config.default_config_path", lambda: None, raising=True)
    init_db()


def _doc(**over):
    """A valid manifest. ``route.model`` is the gateway's always-present echo route."""
    d = {
        "schema_version": 1,
        "name": APP,
        "route": {"model": "default", "key_ref": f"gateway/{APP}"},
        "rag": {"kb": "hpc-docs", "retrieval": "hybrid", "top_k": 5, "encoder": "token-hash"},
        "prompt": {"name": "hpc-docs-system", "label": "prod"},
        "guardrail": {"mode": "enforce", "policy": "default"},
        "budgets": {"max_tokens": 2048, "max_cost_usd": 5.0},
        "eval": {"suites": ["hpc-docs-groundedness@1"]},
    }
    d.update(over)
    return d


def _seed_prompt(name="hpc-docs-system", label="prod"):
    v = create_prompt_version(name, "Answer using {context}.", variables=["context"], actor="t")
    set_prompt_label(name, label, v)
    return v


def _seed_kb(kb="hpc-docs", tenant="default", encoder="token-hash"):
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rag_kbs (kb, tenant, source_revision, encoder, chunk_count) "
            "VALUES (?,?,?,?,?)",
            (kb, tenant, "rev-1", encoder, 3),
        )


def _seed_components():
    _seed_prompt()
    _seed_kb()


def _events(prefix: str):
    return [e for e in export_audit_events() if str(e["action"]).startswith(prefix)]


# -- manifest: canonicalisation and hash stability ---------------------------------------------


def test_version_id_is_stable_under_key_order_and_defaults():
    a = ga.normalize(_doc())
    reordered = json.loads(json.dumps(_doc(), sort_keys=True))
    b = ga.normalize(reordered)
    assert ga.version_id_of(a) == ga.version_id_of(b)
    assert ga.normalize(a) == a  # idempotent: a stored manifest re-validates to itself
    assert ga.version_id_of(a).startswith("gaa-sha256:")
    # A default spelled out and a default omitted are the same application.
    terse = _doc(rag={"kb": "hpc-docs", "retrieval": "hybrid", "encoder": "token-hash"})
    assert ga.normalize(terse)["rag"]["top_k"] == 5
    assert ga.version_id_of(ga.normalize(terse)) == ga.version_id_of(a)
    assert ga.normalize(_doc(guardrail={"mode": "enforce"}))["guardrail"]["policy"] == "default"


def test_changing_any_referenced_component_is_a_new_version():
    base = ga.version_id_of(ga.normalize(_doc()))
    variants = [
        _doc(route={"model": "other-model", "key_ref": f"gateway/{APP}"}),
        _doc(rag={"kb": "other-kb", "retrieval": "hybrid", "top_k": 5, "encoder": "token-hash"}),
        _doc(prompt={"name": "hpc-docs-system", "version": 7}),
        _doc(guardrail={"mode": "monitor", "policy": "default"}),
        _doc(eval={"suites": ["hpc-docs-groundedness@2"]}),
    ]
    ids = {ga.version_id_of(ga.normalize(v)) for v in variants}
    assert base not in ids and len(ids) == len(variants)


def test_an_id_that_does_not_match_the_content_is_refused_not_corrected():
    stale = _doc(version_id="gaa-sha256:" + "0" * 64)
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(stale)
    assert "does not match the content" in str(exc.value)


def test_a_stored_version_is_immutable_and_re_register_is_idempotent():
    first = ga.register(_doc())
    again = ga.register(json.loads(json.dumps(_doc(), sort_keys=True)))
    assert first["created"] is True and again["created"] is False
    assert first["version_id"] == again["version_id"]
    assert len(store.list_versions(APP)) == 1
    assert len(_events("genai_app_registered")) == 1  # the no-op re-register is not an event
    # There is no update helper: the storage module exposes insert only.
    assert not [n for n in dir(store) if n.startswith("update")]


# -- manifest: refusals ------------------------------------------------------------------------


def test_a_prompt_reference_that_is_neither_pinned_nor_labelled_is_refused():
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(_doc(prompt={"name": "hpc-docs-system"}))
    assert any("'label'" in p and "'version'" in p for p in exc.value.problems)


def test_a_prompt_reference_cannot_be_both_pinned_and_labelled():
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(_doc(prompt={"name": "p", "label": "prod", "version": 3}))
    assert any("mutually exclusive" in p for p in exc.value.problems)


@pytest.mark.parametrize(
    "model",
    ["https://api.example.com/v1", "ollama://llama3", "gpu01.example.org:11434", "two words"],
)
def test_a_route_naming_a_provider_address_is_refused(model):
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(_doc(route={"model": model, "key_ref": f"gateway/{APP}"}))
    assert any("logical-model name" in p for p in exc.value.problems)


def test_a_key_ref_must_be_a_gateway_reference_never_a_raw_credential():
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(_doc(route={"model": "default", "key_ref": "exa-AbCdEf123456"}))
    assert any("route.key_ref" in p for p in exc.value.problems)


def test_every_problem_is_reported_at_once_and_unknown_fields_are_refused():
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize({"schema_version": 1, "name": "X!", "colour": "blue", "route": {}})
    problems = exc.value.problems
    assert any("colour: unknown field" in p for p in problems)
    assert any(p.startswith("prompt: required") for p in problems)
    assert any(p.startswith("guardrail: required") for p in problems)
    assert any("name:" in p for p in problems)  # uppercase + '!' fails the name pattern


def test_a_bad_guardrail_mode_or_retrieval_mode_is_refused():
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(_doc(guardrail={"mode": "lenient"}))
    assert any("guardrail.mode" in p for p in exc.value.problems)
    with pytest.raises(ga.GenAIAppManifestError) as exc:
        ga.normalize(_doc(rag={"kb": "k", "retrieval": "sparse"}))
    assert any("rag.retrieval" in p for p in exc.value.problems)


def test_rag_is_optional_and_its_absence_is_a_legal_application():
    """ADR 0159 decision 5: partial composition is legal; a *declared* missing component is not."""
    _seed_prompt()  # no KB is ingested anywhere in this test
    doc = _doc()
    del doc["rag"]
    m = ga.normalize(doc)
    assert "rag" not in m
    assert ga.component_refusals(m, tenant="default") == []
    # The same manifest *with* the rag block is refused, so the empty list above is a real answer.
    assert [r.component for r in ga.component_refusals(ga.normalize(_doc()))] == ["rag.kb"]


# -- read paths --------------------------------------------------------------------------------


def test_register_show_list_round_trip():
    vid = ga.register(_doc())["version_id"]
    assert ga.get(vid)["manifest"]["route"]["model"] == "default"
    rows = ga.list_apps(APP)
    assert rows[0]["version_id"] == vid and rows[0]["rag"] == "hpc-docs"
    assert rows[0]["aliases"] == []
    ga.set_alias(APP, "Staging", vid)
    assert ga.list_apps(APP)[0]["aliases"] == ["Staging"]
    assert ga.resolve(APP, "Staging").version_id == vid
    assert ga.get(f"{APP}@Staging")["version_id"] == vid
    assert ga.get(f"{APP}@Nope") is None
    with pytest.raises(LookupError):
        ga.resolve(APP, "Production")


def test_diff_names_the_components_that_changed():
    a = ga.register(_doc())["version_id"]
    b = ga.register(_doc(guardrail={"mode": "monitor", "policy": "default"}))["version_id"]
    out = ga.diff(a, b)
    assert out["identical"] is False
    assert [c["component"] for c in out["changes"]] == ["guardrail.mode"]
    assert ga.diff(a, a)["identical"] is True


# -- the promotion gate: evidence --------------------------------------------------------------


def _gate(mode="block", suite="hpc-docs-groundedness", metric="groundedness"):
    set_eval_gate(KEY, suite, [{"name": metric, "min": 0.8}], mode=mode, updated_by="test")


def _result(vid, *, judge=None, score=0.9, suite="hpc-docs-groundedness", metric="groundedness"):
    record_eval_result(
        suite,
        KEY,
        {metric: score},
        run_id=f"run-{vid[-6:]}-{score}",
        model_version=vid,
        sample_size=100,
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
    with pytest.raises(ga.GateRefusal) as exc:
        ga.set_alias(APP, "Production", vid)
    return exc.value.reasons


def test_production_is_refused_when_there_is_no_evaluation_evidence():
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    assert any("no evaluation evidence" in r for r in _refused(vid))  # no gate at all
    _gate()
    assert any("no evaluation evidence" in r and "no results" in r for r in _refused(vid))
    assert store.get_alias(APP, "Production") is None  # nothing moved
    blocked = _events("genai_app_promotion_blocked")
    assert len(blocked) == 2 and "no evaluation evidence" in json.dumps(blocked[-1]["details"])


def test_a_warn_mode_gate_cannot_promote():
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _gate(mode="warn")
    _result(vid)
    assert any("warn mode" in r for r in _refused(vid))


def test_an_uncalibrated_judge_blocks_production_and_is_named():
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _gate()
    _result(vid, judge="ghost-judge")
    assert any("ghost-judge" in r and "no_calibration" in r for r in _refused(vid))
    record_judge_calibration(_cal("ghost-judge", position_bias=0.19))
    assert any("position_bias" in r for r in _refused(vid))


def test_the_gate_is_the_platforms_one_gate_not_a_second_one():
    """The agent-version registry and this one call the same function, on the same key shape."""
    from examlops.agent_versions import service as av_service
    from examlops.evaluation import evidence as shared

    assert ga.model_key(APP) == f"genai-app-{APP}"
    assert av_service.model_key("jobdoc") == "agent-jobdoc"
    src_av = av_service.evidence_refusals.__doc__ or ""
    assert "evaluation_evidence" in src_av
    assert callable(shared.evaluation_evidence)


def test_production_moves_with_a_calibrated_judge_and_records_the_evidence():
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _gate()
    record_judge_calibration(_cal("good-judge"))
    _result(vid, judge="good-judge", score=0.93)
    out = ga.set_alias(APP, "Production", vid, reason="evals green")
    assert out["evidence"]["gate_passed"] is True and out["evidence"]["calibration_id"]
    assert ga.resolve(APP).version_id == vid
    moved = _events("genai_app_alias_moved")[-1]
    assert moved["target"] == f"{APP}@Production"
    assert store.alias_history(APP, "Production")[0]["evidence_json"]


def test_rollback_restores_the_previous_version_and_is_not_regated():
    _seed_components()
    v1 = ga.register(_doc())["version_id"]
    v2 = ga.register(_doc(budgets={"max_tokens": 4096}))["version_id"]
    _gate()
    _result(v1)
    _result(v2)
    ga.set_alias(APP, "Production", v1)
    ga.set_alias(APP, "Production", v2)
    set_eval_gate(KEY, "hpc-docs-groundedness", [{"name": "groundedness", "min": 2.0}])
    assert ga.rollback(APP, "Production")["version_id"] == v1
    assert [h["action"] for h in store.alias_history(APP, "Production")] == [
        "rollback",
        "set",
        "set",
    ]


def test_a_staging_move_is_not_gated_at_all():
    vid = ga.register(_doc())["version_id"]  # no gate, no components, no evidence
    assert ga.set_alias(APP, "Staging", vid)["ok"]
    assert ga.set_alias(APP, "Canary", vid)["ok"]


# -- the promotion gate: this registry's own two clauses ---------------------------------------


def _pass_the_eval_gate(vid):
    _gate()
    record_judge_calibration(_cal("good-judge"))
    _result(vid, judge="good-judge", score=0.93)


def test_a_production_application_may_not_run_with_guardrails_off():
    _seed_components()
    vid = ga.register(_doc(guardrail={"mode": "off", "policy": "default"}))["version_id"]
    _pass_the_eval_gate(vid)
    reasons = _refused(vid)
    assert any("guardrail.mode is 'off'" in r for r in reasons)
    assert store.get_alias(APP, "Production") is None


def test_a_dangling_rag_reference_refuses_promotion_with_a_named_reason():
    _seed_prompt()  # the KB is deliberately never ingested
    vid = ga.register(_doc())["version_id"]
    _pass_the_eval_gate(vid)
    reasons = _refused(vid)
    assert any(ga.NOT_FOUND in r and "rag.kb" in r and "hpc-docs" in r for r in reasons)


def test_an_encoder_that_does_not_match_the_knowledge_base_refuses_promotion():
    _seed_prompt()
    _seed_kb(encoder="bge-small")
    vid = ga.register(_doc())["version_id"]
    _pass_the_eval_gate(vid)
    assert any("rag.encoder" in r and "bge-small" in r for r in _refused(vid))


def test_a_prompt_label_that_no_longer_resolves_refuses_promotion():
    _seed_kb()  # the prompt is deliberately never created
    vid = ga.register(_doc())["version_id"]
    _pass_the_eval_gate(vid)
    assert any(ga.NOT_FOUND in r and "prompt" in r for r in _refused(vid))


def test_a_pinned_prompt_version_is_checked_by_number():
    _seed_kb()
    _seed_prompt()  # creates version 1 only
    vid = ga.register(_doc(prompt={"name": "hpc-docs-system", "version": 99}))["version_id"]
    _pass_the_eval_gate(vid)
    assert any("hpc-docs-system@v99" in r for r in _refused(vid))


def test_an_unknown_gateway_route_refuses_promotion():
    _seed_components()
    doc = _doc(route={"model": "no-such-route", "key_ref": f"gateway/{APP}"})
    vid = ga.register(doc)["version_id"]
    _pass_the_eval_gate(vid)
    assert any(ga.NOT_FOUND in r and "route.model" in r for r in _refused(vid))


# -- fail closed: an unresolvable guardrail is never silently skipped ---------------------------


def test_an_unknown_guardrail_policy_is_refused_never_run_as_the_default():
    _seed_components()
    vid = ga.register(_doc(guardrail={"mode": "enforce", "policy": "strict"}))["version_id"]
    _pass_the_eval_gate(vid)
    reasons = _refused(vid)
    assert any(ga.NOT_FOUND in r and "guardrail.policy" in r and "strict" in r for r in reasons)
    assert store.get_alias(APP, "Production") is None


def test_an_unreachable_guardrail_subsystem_refuses_the_promotion(monkeypatch):
    """ADR 0159 decision 5: a question that cannot be answered is a refusal, not a pass."""
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _pass_the_eval_gate(vid)

    def boom():
        raise RuntimeError("guardrail service unavailable")

    monkeypatch.setattr("examlops.genai_apps.components.known_guardrail_policies", boom)
    reasons = _refused(vid)
    assert any(ga.UNREACHABLE in r and "guardrail.policy" in r for r in reasons)
    assert any("never run unguarded" in r for r in reasons)
    assert store.get_alias(APP, "Production") is None


def test_an_unreadable_component_store_refuses_rather_than_passing(monkeypatch):
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _pass_the_eval_gate(vid)

    def boom(*_a, **_k):
        raise RuntimeError("datastore unavailable")

    monkeypatch.setattr("examlops.data.prompts.get_prompt_by_label", boom)
    assert any(ga.UNREACHABLE in r and "prompt" in r for r in _refused(vid))


def test_a_broken_gateway_config_refuses_rather_than_passing(monkeypatch, tmp_path):
    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _pass_the_eval_gate(vid)
    bad = tmp_path / "gateway.yaml"
    bad.write_text("version: 1\nproviders: {}\nmodels: {}\naliases: {a: nope}\n")
    monkeypatch.setattr("examlops.gateway.config.default_config_path", lambda: bad)
    assert any(ga.UNREACHABLE in r and "route.model" in r for r in _refused(vid))


def test_component_refusals_checks_every_declared_reference_and_skips_none():
    """All four wrong at once: four named refusals, not one lucky short-circuit."""
    doc = ga.normalize(
        _doc(
            route={"model": "no-such-route", "key_ref": f"gateway/{APP}"},
            rag={"kb": "no-such-kb", "retrieval": "dense", "top_k": 1},
            prompt={"name": "no-such-prompt", "label": "prod"},
            guardrail={"mode": "enforce", "policy": "no-such-policy"},
        )
    )
    got = {r.component for r in ga.component_refusals(doc)}
    assert got == {"route.model", "rag.kb", "prompt", "guardrail.policy"}
    # An undeclared component is not a missing one: drop `rag` and it drops out of the answer.
    doc.pop("rag")
    assert "rag.kb" not in {r.component for r in ga.component_refusals(doc)}


def test_a_fully_resolvable_manifest_has_no_component_refusals():
    _seed_components()
    assert ga.component_refusals(ga.normalize(_doc())) == []


# -- CLI ---------------------------------------------------------------------------------------


def _write(tmp_path, doc, name="app.yaml"):
    import yaml

    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(p)


def test_cli_register_show_list_in_json(tmp_path):
    path = _write(tmp_path, _doc())
    res = runner.invoke(app, ["--json", "genai-app", "register", path])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["created"] is True and out["version_id"].startswith("gaa-sha256:")
    vid = out["version_id"]

    res = runner.invoke(app, ["--json", "genai-app", "register", path])
    assert res.exit_code == 0 and json.loads(res.stdout)["created"] is False

    res = runner.invoke(app, ["--json", "genai-app", "show", vid])
    assert res.exit_code == 0
    assert json.loads(res.stdout)["manifest"]["guardrail"]["mode"] == "enforce"

    res = runner.invoke(app, ["--json", "genai-app", "list"])
    assert res.exit_code == 0 and [r["version_id"] for r in json.loads(res.stdout)] == [vid]

    res = runner.invoke(app, ["genai-app", "list"])
    assert res.exit_code == 0 and "GenAI applications" in res.output


def test_cli_an_invalid_manifest_exits_1_and_names_every_problem(tmp_path):
    path = _write(tmp_path, _doc(prompt={"name": "p"}, guardrail={"mode": "nope"}))
    res = runner.invoke(app, ["--json", "genai-app", "register", path])
    assert res.exit_code == 1
    out = json.loads(res.stdout)
    assert out["ok"] is False and out["code"] == "invalid_manifest"
    assert len(out["problems"]) >= 2


def test_cli_show_of_an_unknown_ref_exits_1():
    res = runner.invoke(app, ["--json", "genai-app", "show", "gaa-sha256:" + "0" * 64])
    assert res.exit_code == 1 and json.loads(res.stdout)["code"] == "not_found"


def test_cli_promote_refusal_exits_1_and_lists_the_reasons(tmp_path):
    _seed_prompt()  # no KB: the component check will refuse
    path = _write(tmp_path, _doc())
    vid = json.loads(runner.invoke(app, ["--json", "genai-app", "register", path]).stdout)[
        "version_id"
    ]
    _pass_the_eval_gate(vid)
    res = runner.invoke(app, ["--json", "--yes", "genai-app", "promote", APP, "Production", vid])
    assert res.exit_code == 1
    out = json.loads(res.stdout)
    assert out["code"] == "promotion_refused"
    assert any("rag.kb" in r for r in out["reasons"])


def test_cli_promote_succeeds_and_reports_the_previous_pointer(tmp_path):
    _seed_components()
    path = _write(tmp_path, _doc())
    vid = json.loads(runner.invoke(app, ["--json", "genai-app", "register", path]).stdout)[
        "version_id"
    ]
    _pass_the_eval_gate(vid)
    res = runner.invoke(app, ["--json", "--yes", "genai-app", "promote", APP, "Staging", vid])
    assert res.exit_code == 0 and json.loads(res.stdout)["previous"] is None
    res = runner.invoke(
        app, ["--json", "--yes", "genai-app", "promote", APP, "Production", f"{APP}@Staging"]
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["evidence"]["gate_passed"] is True


def test_cli_an_unknown_alias_exits_1():
    res = runner.invoke(app, ["--json", "--yes", "genai-app", "promote", APP, "Live", "x"])
    assert res.exit_code == 1 and json.loads(res.stdout)["code"] == "invalid_alias"


# -- a lost audit event is counted, not passed over ---------------------------------------------
#
# This registry's writes fail open through `audit_best_effort` (a registry write must not be undone
# because the audit datastore blinked), so the only thing that can tell the log is incomplete is
# the drop counter the control plane publishes as `audit_events_dropped`. Each test below breaks
# the audit append the way an outage would, checks the registry still did its real work, and then
# checks the loss was *counted* — the half that a bare `except Exception: pass` throws away.


def _break_audit(monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import reset_dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit down")

    reset_dropped_audit_events()  # process-global, like the Prometheus registry
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_register_audit_is_counted(monkeypatch):
    from examlops.data.audit import dropped_audit_events

    _break_audit(monkeypatch)
    out = ga.register(_doc())
    assert out["created"] is True  # the version was still stored
    assert store.get_version(out["version_id"])["name"] == APP
    assert dropped_audit_events().get("genai_app_registered") == 1


def test_a_lost_alias_move_audit_is_counted(monkeypatch):
    """Both of `set_alias`'s audit sites: the move that happened, and the promotion that did not.

    A refusal is the more dangerous of the two to lose — an unrecorded block reads, afterwards,
    exactly like a block that never happened — so it is checked here that the refusal still stands
    *and* that its lost record is counted.
    """
    from examlops.data.audit import dropped_audit_events

    _seed_components()
    vid = ga.register(_doc())["version_id"]
    _break_audit(monkeypatch)

    ga.set_alias(APP, "Staging", vid)
    assert ga.resolve(APP, "Staging").version_id == vid  # the alias really moved
    assert dropped_audit_events().get("genai_app_alias_moved") == 1

    with pytest.raises(ga.GateRefusal):  # a lost refusal audit must not turn a refusal into a pass
        ga.set_alias(APP, "Production", vid)
    assert store.get_alias(APP, "Production") is None
    assert dropped_audit_events().get("genai_app_promotion_blocked") == 1


def test_a_lost_rollback_audit_is_counted(monkeypatch):
    from examlops.data.audit import dropped_audit_events

    _seed_components()
    v1 = ga.register(_doc())["version_id"]
    v2 = ga.register(_doc(budgets={"max_tokens": 4096}))["version_id"]
    ga.set_alias(APP, "Staging", v1)
    ga.set_alias(APP, "Staging", v2)
    _break_audit(monkeypatch)

    assert ga.rollback(APP, "Staging")["version_id"] == v1
    assert ga.resolve(APP, "Staging").version_id == v1  # the pointer really went back
    assert dropped_audit_events().get("genai_app_alias_rolled_back") == 1

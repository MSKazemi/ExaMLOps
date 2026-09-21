"""ADR 0029 / 0079 completion — rollout modes, armed engine gates, more gated commands, the
``exa.providers.policy`` plugin group, the shipped Rego bundle, and the datasheet lint.

House style of ``test_policy_manual_gates.py``: real policy files under ``tmp_path``, the real CLI
runner and audit chain; only the MLflow HTTP client and OS-level effects are faked. Every
behaviour-preservation test asserts the *default* (no file / gate off) is byte-identical and writes
no ``policy*`` audit row.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

REPO = Path(__file__).parents[2]
sys.path.insert(0, str(REPO / "platform" / "cli" / "src"))
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.delenv("EXAMLOPS_POLICY_GATES", raising=False)
    monkeypatch.delenv("EXAMLOPS_POLICY_ENGINE", raising=False)
    monkeypatch.delenv("EXAMLOPS_PROJECT", raising=False)
    from examlops import platform_db

    platform_db.init_db()
    yield tmp_path
    # `exa pipeline run --project X` sets os.environ["EXAMLOPS_PROJECT"] for the run, and
    # monkeypatch records nothing to undo for a variable that was absent — so pop it explicitly or
    # it leaks into whichever test the same xdist worker runs next.
    os.environ.pop("EXAMLOPS_PROJECT", None)


def _policy(tmp_path, monkeypatch, text):
    path = tmp_path / "policy.yaml"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr("examlops.policy.POLICY_YAML", path)
    return path


def _events(prefix):
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if str(e["action"]).startswith(prefix)]


def _promote(*cmd_flags):
    args = ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0", *cmd_flags]
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, args)
    return res, post


# ── (3) per-policy rollout: monitor | enforce ───────────────────────────────────────────────
_DENY_MONITOR = (
    "policies:\n  - name: trial\n    action: retrain\n    effect: deny\n    mode: monitor\n"
)


def test_monitor_rule_never_blocks_but_is_recorded_as_shadow(env, monkeypatch):
    from examlops import policy

    rules = [{"name": "trial", "action": "retrain", "effect": "deny", "mode": "monitor"}]
    d = policy.decide("retrain", {}, policies=rules, audit=False)
    assert d.effect == policy.ALLOW and d.rule is None
    assert d.shadow == (("trial", "deny"),)


def test_monitor_then_enforce_later_rule_still_decides(env):
    from examlops import policy

    rules = [
        {"name": "trial", "action": "retrain", "effect": "deny", "mode": "monitor"},
        {"name": "real", "action": "retrain", "effect": "require_approval"},
    ]
    d = policy.decide("retrain", {}, policies=rules, audit=False)
    assert (d.effect, d.rule) == (policy.REQUIRE_APPROVAL, "real")
    assert d.shadow == (("trial", "deny"),)


def test_enforce_mode_is_the_default_and_blocks(env):
    from examlops import policy

    for rule in (
        {"action": "retrain", "effect": "deny"},
        {"action": "retrain", "effect": "deny", "mode": "enforce"},
    ):
        d = policy.decide("retrain", {}, policies=[rule], audit=False)
        assert d.denied and d.shadow == ()


def test_unknown_mode_fails_closed_to_enforce(env):
    from examlops import policy

    d = policy.decide(
        "retrain",
        {},
        policies=[{"action": "retrain", "effect": "deny", "mode": "moniter"}],
        audit=False,
    )
    assert d.denied


def test_monitor_decision_is_audited_with_would_effect(env, monkeypatch):
    from examlops import policy

    d = policy.decide(
        "retrain",
        {"model": "M"},
        policies=[{"name": "trial", "action": "retrain", "effect": "deny", "mode": "monitor"}],
    )
    assert d.allowed
    rows = _events("policy_monitor:retrain")
    assert len(rows) == 1
    details = json.loads(rows[0]["details"])
    assert details["would_effect"] == "deny" and details["enforced"] is False


def test_monitor_rule_through_cli_promote_proceeds_and_audits(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        _DENY_MONITOR.replace("retrain", "manual_promote"),
    )
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert post.called  # never blocked
    assert len(_events("policy_monitor:manual_promote")) == 1
    assert _events("policy:manual_promote") == []  # no enforcing rule made a decision


def test_policy_list_shows_mode_column(env, monkeypatch):
    _policy(env, monkeypatch, _DENY_MONITOR)
    res = runner.invoke(app, ["--json", "policy", "list"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["policies"][0]["mode"] == "monitor"


def test_gates_only_policy_file_is_valid_not_an_error(env, monkeypatch):
    from examlops.policy import load_policies_with_status

    path = _policy(env, monkeypatch, "gates:\n  supply_chain: monitor\n")
    assert load_policies_with_status(path) == ([], None)


# ── (1) engine gates: off by default, monitor, enforce ──────────────────────────────────────
def test_gates_default_off_and_env_beats_file(env, monkeypatch):
    from examlops.policy_engine import gates

    _policy(env, monkeypatch, None)
    assert {g["mode"] for g in gates.configured_gates().values()} == {"off"}
    _policy(env, monkeypatch, "gates:\n  supply_chain: monitor\n  model_card: {mode: enforce}\n")
    assert gates.gate_mode("supply_chain") == "monitor"
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "supply_chain=enforce,budget=monitor")
    assert gates.gate_mode("supply_chain") == "enforce"
    assert gates.gate_mode("budget") == "monitor"
    assert gates.gate_mode("model_card") == "enforce"


def test_gate_unknown_mode_fails_closed(env, monkeypatch):
    from examlops.policy_engine import gates

    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "supply_chain=warn-ish")
    assert gates.gate_mode("supply_chain") == "enforce"


def test_promote_gates_off_is_byte_identical(env, monkeypatch):
    _policy(env, monkeypatch, None)
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert post.called
    assert _events("policy") == []


def test_promote_unsigned_denied_when_supply_chain_gate_enforced(env, monkeypatch):
    _policy(env, monkeypatch, "gates:\n  supply_chain: enforce\n")
    res, post = _promote()
    assert res.exit_code == 1, res.output
    assert "unsigned" in res.output
    assert not post.called
    assert len(_events("policy_supply_chain")) == 1


def test_promote_unsigned_only_recorded_when_gate_monitored(env, monkeypatch):
    _policy(env, monkeypatch, "gates:\n  supply_chain: monitor\n")
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert post.called
    assert len(_events("policy_gate_monitor:supply_chain")) == 1


def test_promote_signed_passes_enforced_supply_chain_gate(env, monkeypatch):
    from examlops.data import registry

    monkeypatch.setattr(registry, "get_model_signature", lambda m, v: {"digest": "d"})
    _policy(env, monkeypatch, "gates:\n  supply_chain: enforce\n")
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert post.called


def test_promote_incomplete_card_denied_by_enforced_card_gate(env, monkeypatch):
    monkeypatch.setattr("examlops.cards.card_completeness", lambda model, tenant="default": 0.4)
    _policy(env, monkeypatch, "gates:\n  model_card: enforce\n")
    res, post = _promote()
    assert res.exit_code == 1, res.output
    assert "completeness" in res.output
    assert not post.called


def test_promote_card_gate_floor_option_and_force_does_not_override(env, monkeypatch):
    monkeypatch.setattr("examlops.cards.card_completeness", lambda model, tenant="default": 0.85)
    _policy(env, monkeypatch, "gates:\n  model_card: {mode: enforce, floor: 0.9}\n")
    res, post = _promote("--force")
    assert res.exit_code == 1, res.output
    assert not post.called
    _policy(env, monkeypatch, "gates:\n  model_card: {mode: enforce, floor: 0.8}\n")
    res, post = _promote()
    assert res.exit_code == 0 and post.called


def test_promote_unmeasurable_card_is_not_a_complete_one(env, monkeypatch):
    def boom(model, tenant="default"):
        raise RuntimeError("db down")

    monkeypatch.setattr("examlops.cards.card_completeness", boom)
    _policy(env, monkeypatch, "gates:\n  model_card: enforce\n")
    res, post = _promote()
    assert res.exit_code == 1 and not post.called


def _sign(tmp_path, monkeypatch, model="M", version="1", tamper=False):
    from examlops.supplychain import sign_model

    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-key-not-a-secret")
    art = tmp_path / "w.bin"
    art.write_bytes(b"weights")
    sign_model(model, version, [art], actor="t", root=tmp_path)
    if tamper:
        art.write_bytes(b"tampered")
    return art


def test_verify_before_load_default_unchanged_and_gate_can_only_tighten(env, monkeypatch):
    from examlops.supplychain import verify_before_load

    art = _sign(env, monkeypatch, tamper=True)  # signed, then modified: fails verification
    _policy(env, monkeypatch, None)
    assert verify_before_load("M", "1", [art], mode="warn", root=env) is True  # warn loads
    _policy(env, monkeypatch, "gates:\n  supply_chain: enforce\n")
    assert verify_before_load("M", "1", [art], mode="warn", root=env) is False  # gate refuses
    _policy(env, monkeypatch, "gates:\n  supply_chain: monitor\n")
    assert verify_before_load("M", "1", [art], mode="warn", root=env) is True


def test_verify_before_load_verified_artifact_passes_enforced_gate(env, monkeypatch):
    from examlops.supplychain import verify_before_load

    art = _sign(env, monkeypatch)
    _policy(env, monkeypatch, "gates:\n  supply_chain: enforce\n")
    assert verify_before_load("M", "1", [art], mode="enforce", root=env) is True


def _project_over_budget(monkeypatch):
    from examlops import project_finops
    from examlops.data.projects import create_project, set_project_budget

    create_project("acme")
    set_project_budget("acme", 10.0, None, period="total", updated_by="t")
    monkeypatch.setattr(
        project_finops,
        "get_project_consumption",
        lambda *a, **k: {"gpu_hours": 50.0, "cost_usd": 0.0},
    )


def _run(*args):
    with patch("examlops.cli.commands.pipeline._run_generator") as gen:
        res = runner.invoke(app, ["pipeline", "run", *args])
    return res, gen


def test_budget_gate_off_by_default_runs_over_budget_project(env, monkeypatch):
    _policy(env, monkeypatch, None)
    _project_over_budget(monkeypatch)
    res, gen = _run("--project", "acme", "--model", "JPCP", "--dummy")
    assert res.exit_code == 0, res.output
    assert gen.called and _events("policy") == []


def test_budget_gate_enforced_blocks_over_budget_run(env, monkeypatch):
    _policy(env, monkeypatch, "gates:\n  budget: enforce\n")
    _project_over_budget(monkeypatch)
    res, gen = _run("--project", "acme", "--dummy")
    assert res.exit_code == 1, res.output
    assert "budget" in res.output
    assert not gen.called
    assert len(_events("policy_budget")) == 1


def test_budget_gate_monitor_lets_run_proceed_and_records(env, monkeypatch):
    _policy(env, monkeypatch, "gates:\n  budget: monitor\n")
    _project_over_budget(monkeypatch)
    res, gen = _run("--project", "acme", "--dummy")
    assert res.exit_code == 0, res.output
    assert gen.called
    assert len(_events("policy_gate_monitor:budget")) == 1


def test_budget_gate_no_budget_or_no_project_is_allowed(env, monkeypatch):
    _policy(env, monkeypatch, "gates:\n  budget: enforce\n")
    res, gen = _run("--dummy")  # no project at all
    assert res.exit_code == 0 and gen.called
    from examlops.data.projects import create_project

    create_project("nobudget")
    res, gen = _run("--project", "nobudget", "--dummy")
    assert res.exit_code == 0 and gen.called


# ── (2) more gated mutating commands ────────────────────────────────────────────────────────
def _deny(action):
    return f"policies:\n  - name: freeze\n    action: {action}\n    effect: deny\n"


def _approval(action):
    return f"policies:\n  - action: {action}\n    effect: require_approval\n"


def _register(name="lxp"):
    from examlops import hpc_registry

    hpc_registry.register_pending(
        name,
        "flux",
        transport="ssh",
        host="h",
        ssh_user="u",
        ssh_port=22,
        ssh_key="k",
        capabilities={"scheduler": "flux"},
        requested_by="t",
    )


def _state(name="lxp"):
    from examlops.data.hpc import get_clusters

    return next(c["state"] for c in get_clusters() if c["name"] == name)


def _fake_probe(monkeypatch):
    disc = SimpleNamespace(
        _PROBES={},
        probe_scheduler=lambda ex: SimpleNamespace(to_dict=lambda: {"scheduler": "flux"}),
    )
    monkeypatch.setattr("examlops.cli.commands.hpc_cmd._load_discovery", lambda: (disc,))
    build = MagicMock()
    monkeypatch.setattr("examlops.cli.commands.hpc_cmd._build_executor", build)
    return build


def test_connect_default_unchanged_no_policy_row(env, monkeypatch):
    _policy(env, monkeypatch, None)
    _fake_probe(monkeypatch)
    res = runner.invoke(app, ["hpc", "connect", "login1", "--name", "c1"])
    assert res.exit_code == 0, res.output
    assert _state("c1") == "PENDING"
    assert _events("policy") == []


def test_connect_deny_blocks_before_probing_or_registering(env, monkeypatch):
    _policy(env, monkeypatch, _deny("connect_cluster"))
    build = _fake_probe(monkeypatch)
    res = runner.invoke(app, ["hpc", "connect", "login1", "--name", "c1"])
    assert res.exit_code == 1 and "freeze" in res.output
    assert not build.called
    from examlops.data.hpc import get_clusters

    assert all(c["name"] != "c1" for c in get_clusters())
    assert len(_events("policy:connect_cluster")) == 1


def test_connect_require_approval_registers_pending_with_note(env, monkeypatch):
    _policy(env, monkeypatch, _approval("connect_cluster"))
    _fake_probe(monkeypatch)
    res = runner.invoke(app, ["hpc", "connect", "login1", "--name", "c1"])
    assert res.exit_code == 0 and _state("c1") == "PENDING"
    assert "Policy requires approval" in res.output


def test_reject_default_unchanged(env, monkeypatch):
    _policy(env, monkeypatch, None)
    _register()
    res = runner.invoke(app, ["hpc", "reject", "lxp", "--reason", "x"])
    assert res.exit_code == 0, res.output
    assert _state() == "REJECTED" and _events("policy") == []


def test_reject_deny_and_approval_flow(env, monkeypatch):
    _policy(env, monkeypatch, _deny("cluster_reject"))
    _register()
    res = runner.invoke(app, ["hpc", "reject", "lxp"])
    assert res.exit_code == 1 and _state() == "PENDING"
    _policy(env, monkeypatch, _approval("cluster_reject"))
    res = runner.invoke(app, ["hpc", "reject", "lxp"], input="\n")
    assert res.exit_code == 0 and _state() == "PENDING"  # default answer is no
    res = runner.invoke(app, ["hpc", "reject", "lxp"], input="y\n")
    assert _state() == "REJECTED"


def test_model_sign_gated_and_default_unchanged(env, monkeypatch):
    fake = MagicMock(
        return_value=SimpleNamespace(algo="hmac", digest="d" * 30, model="M", version="1")
    )
    monkeypatch.setattr("examlops.supplychain.sign_registered_version", fake)
    _policy(env, monkeypatch, None)
    res = runner.invoke(app, ["models", "sign", "M", "1"])
    assert res.exit_code == 0 and fake.call_count == 1 and _events("policy") == []
    _policy(env, monkeypatch, _deny("model_sign"))
    res = runner.invoke(app, ["models", "sign", "M", "1"])
    assert res.exit_code == 1 and fake.call_count == 1  # not called again


def test_secret_rotate_gated(env, monkeypatch):
    rot = MagicMock(return_value=2)
    monkeypatch.setattr("examlops.secrets.rotate_secret", rot)
    _policy(env, monkeypatch, None)
    assert runner.invoke(app, ["secrets", "rotate", "a/b"]).exit_code == 0
    assert rot.call_count == 1 and _events("policy") == []
    _policy(env, monkeypatch, _deny("secret_rotate"))
    assert runner.invoke(app, ["secrets", "rotate", "a/b"]).exit_code == 1
    assert rot.call_count == 1
    _policy(env, monkeypatch, _approval("secret_rotate"))
    assert runner.invoke(app, ["secrets", "rotate", "a/b"], input="\n").exit_code == 0
    assert rot.call_count == 1
    assert runner.invoke(app, ["secrets", "rotate", "a/b"], input="y\n").exit_code == 0
    assert rot.call_count == 2


def _mk_project(name="acme"):
    from examlops.data.projects import create_project

    create_project(name)


def _project_exists(name="acme"):
    from examlops.data.projects import get_project

    return get_project(name) is not None


def test_project_delete_default_and_deny(env, monkeypatch):
    _policy(env, monkeypatch, None)
    _mk_project()
    res = runner.invoke(app, ["project", "delete", "acme", "--yes"])
    assert res.exit_code == 0 and not _project_exists() and _events("policy") == []
    _mk_project()
    _policy(env, monkeypatch, _deny("project_delete"))
    res = runner.invoke(app, ["project", "delete", "acme", "--yes"])
    assert res.exit_code == 1 and _project_exists()


def test_project_delete_require_approval_prompt_default_no(env, monkeypatch):
    _mk_project()
    _policy(env, monkeypatch, _approval("project_delete"))
    res = runner.invoke(app, ["project", "delete", "acme"], input="\n")
    assert _project_exists() and "policy requires approval" in res.output
    runner.invoke(app, ["project", "delete", "acme"], input="y\n")
    assert not _project_exists()


def test_project_archive_deny(env, monkeypatch):
    _mk_project()
    _policy(env, monkeypatch, _deny("project_archive"))
    res = runner.invoke(app, ["project", "archive", "acme", "--yes"])
    assert res.exit_code == 1
    assert _events("project_archived") == []


def test_project_remove_member_gated(env, monkeypatch):
    _mk_project()
    rm = MagicMock(return_value=1)
    monkeypatch.setattr("examlops.cli.commands.project_cmd.remove_project_member", rm)
    _policy(env, monkeypatch, None)
    assert runner.invoke(app, ["project", "remove-member", "acme", "bob"]).exit_code == 0
    assert rm.call_count == 1 and _events("policy") == []
    _policy(env, monkeypatch, _deny("project_remove_member"))
    assert runner.invoke(app, ["project", "remove-member", "acme", "bob"]).exit_code == 1
    assert rm.call_count == 1


# ── (4) exa.providers.policy plugin group ───────────────────────────────────────────────────
class _FakeEP:
    def __init__(self, name, target, *, broken=False):
        self.name, self.value, self._t, self._broken = name, "fake:plugin", target, broken

    def load(self):
        if self._broken:
            raise ImportError("plugin import exploded")
        return self._t


def _plugin_class(effect="deny"):
    from examlops.providers import Provider

    class CedarLike(Provider):
        name = "cedar-like"

        def compute(self, inputs):
            self.seen = dict(inputs)
            return {"effect": effect, "reasons": [f"cedar-like says {effect}"]}

    return CedarLike


def _install_eps(monkeypatch, *eps):
    def fake(group):
        return list(eps) if group == "exa.providers.policy" else []

    monkeypatch.setattr("examlops.providers.registry._entry_points", fake)


def test_plugin_engine_selected_by_env_and_used_by_evaluate(env, monkeypatch):
    from examlops.policy_engine import PolicyInput, ProviderEngine, evaluate, get_engine

    _install_eps(monkeypatch, _FakeEP("cedar-like", _plugin_class("deny")))
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "cedar-like")
    assert isinstance(get_engine(), ProviderEngine)
    r = evaluate("promotion", PolicyInput("promote", resource="M/1"), audit=False)
    assert not r.allow and r.engine == "cedar-like" and r.effect == "deny"
    assert "cedar-like says deny" in r.reasons[0]


def test_plugin_receives_the_structured_request(env, monkeypatch):
    from examlops.policy_engine import PolicyInput, get_engine

    cls = _plugin_class("allow")
    _install_eps(monkeypatch, _FakeEP("cedar-like", cls))
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "cedar-like")
    eng = get_engine()
    eng.evaluate("budget", PolicyInput("allocate", subject="u", tenant="t", context={"x": 1}))
    seen = eng.provider.seen
    assert seen["decision"] == "budget" and seen["action"] == "allocate"
    assert seen["tenant"] == "t" and seen["context"]["x"] == 1


def test_plugin_speaking_unknown_effect_is_denied(env, monkeypatch):
    from examlops.policy_engine import PolicyInput, get_engine

    _install_eps(monkeypatch, _FakeEP("cedar-like", _plugin_class("permit")))
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "cedar-like")
    assert not get_engine().evaluate("x", PolicyInput("a")).allow


def test_broken_or_unknown_plugin_degrades_to_yaml_audibly(env, monkeypatch, caplog):
    from examlops.policy_engine import YamlPolicyEngine, get_engine

    _install_eps(monkeypatch, _FakeEP("bad", None, broken=True))
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "bad")
    with caplog.at_level("WARNING"):
        assert isinstance(get_engine(), YamlPolicyEngine)
    assert "falling back to the built-in default" in caplog.text
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "no-such-engine")
    assert isinstance(get_engine(), YamlPolicyEngine)


def test_providers_list_shows_policy_domain_with_broken_plugin_visible(env, monkeypatch):
    _install_eps(
        monkeypatch, _FakeEP("cedar-like", _plugin_class()), _FakeEP("bad", None, broken=True)
    )
    res = runner.invoke(app, ["--json", "providers", "list", "--domain", "policy"])
    assert res.exit_code == 0, res.output
    rows = {r["name"]: r for r in json.loads(res.stdout)}
    assert rows["yaml"]["default"] and rows["yaml"]["ok"]
    assert rows["cedar-like"]["kind"] == "entrypoint" and rows["cedar-like"]["ok"]
    assert rows["bad"]["ok"] is False and "exploded" in rows["bad"]["error"]


def test_default_engine_is_yaml_with_no_env(env):
    from examlops.policy_engine import YamlPolicyEngine, get_engine

    assert isinstance(get_engine(), YamlPolicyEngine)


# ── (5) shipped Rego bundle ─────────────────────────────────────────────────────────────────
BUNDLE = REPO / "platform" / "infra" / "policy-bundle" / "examlops"


def test_rego_bundle_structure():
    from examlops.policy_engine import FAIL_CLOSED_DECISIONS

    shipped = {p.stem for p in BUNDLE.glob("*.rego") if not p.stem.endswith("_test")}
    assert shipped == {"supply_chain", "budget", "model_card"}
    assert shipped <= FAIL_CLOSED_DECISIONS | {"model_card"}
    for name in shipped:
        src = (BUNDLE / f"{name}.rego").read_text()
        assert f"package examlops.{name}\n" in src  # the query path RegoPolicyEngine builds
        assert "default allow := false" in src  # deny unless proven
        test_src = (BUNDLE / f"{name}_test.rego").read_text()
        assert f"package examlops.{name}_test" in test_src
        tests = [ln for ln in test_src.splitlines() if ln.startswith("test_")]
        assert len(tests) >= 3
        assert any("not " in t for t in tests), f"{name}: no negative (deny) case"


@pytest.mark.skipif(shutil.which("opa") is None, reason="opa binary not installed")
def test_rego_bundle_unit_tests_pass_under_opa():
    proc = subprocess.run(
        ["opa", "test", str(BUNDLE), "-v"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_rego_engine_builds_the_documented_query_via_an_opa_shim(env, monkeypatch, tmp_path):
    """No opa needed: a shim on PATH proves RegoPolicyEngine's query/parse contract."""
    from examlops.policy_engine import PolicyInput, get_engine

    shim = tmp_path / "bin" / "opa"
    shim.parent.mkdir()
    shim.write_text(
        "#!/bin/sh\n"
        'echo "$@" > "$0.args"\n'
        'cat > "$0.stdin"\n'
        'echo \'{"result":[{"expressions":[{"value":false}]}]}\'\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim.parent}:/usr/bin:/bin")
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "opa")
    monkeypatch.setenv("EXAMLOPS_POLICY_BUNDLE_DIR", str(BUNDLE.parent))
    eng = get_engine()
    assert eng.name == "opa"
    r = eng.evaluate("supply_chain", PolicyInput("deploy", context={"signed": False}))
    assert not r.allow and r.effect == "deny"
    assert "data.examlops.supply_chain.allow" in (shim.parent / "opa.args").read_text()
    assert json.loads((shim.parent / "opa.stdin").read_text())["signed"] is False


def test_opa_requested_without_binary_degrades_to_yaml(env, monkeypatch, tmp_path):
    from examlops.policy_engine import YamlPolicyEngine, get_engine

    monkeypatch.setenv("PATH", str(tmp_path))  # no opa
    monkeypatch.setenv("EXAMLOPS_POLICY_ENGINE", "opa")
    assert isinstance(get_engine(), YamlPolicyEngine)


# ── (6) datasheet lint ──────────────────────────────────────────────────────────────────────
_GOOD_SCHEMA = [{"name": "a", "dataType": "sc:Integer", "description": "jobs per hour"}]


def test_lint_datasheet_findings_unit():
    from examlops.cards import croissant_record, lint_datasheet

    good = croissant_record("D", revision="r1", schema=_GOOD_SCHEMA)
    assert lint_datasheet(good) == []
    bad = croissant_record("D", schema=[])
    findings = " | ".join(lint_datasheet(bad))
    assert "no description" in findings and "unpinned" in findings and "provenance" in findings


def test_cards_lint_cli_exit_codes(env, monkeypatch):
    monkeypatch.setattr("examlops.usecase.dataset_schema", lambda d: _GOOD_SCHEMA)
    ok = runner.invoke(app, ["--json", "cards", "lint", "D", "--revision", "r1"])
    assert ok.exit_code == 0 and json.loads(ok.stdout)["ok"] is True
    unpinned = runner.invoke(app, ["--json", "cards", "lint", "D"])
    assert unpinned.exit_code == 1 and json.loads(unpinned.stdout)["ok"] is False
    monkeypatch.setattr("examlops.usecase.dataset_schema", lambda d: None)
    undocumented = runner.invoke(app, ["cards", "lint", "D", "--revision", "r1"])
    assert undocumented.exit_code == 1

"""ADR 0079 decision 2 - the manual promote and cluster-approve paths consult ``policy.decide``.

Real policy files under ``tmp_path``, the real CLI runner, the real audit chain. Only the MLflow
HTTP client is faked (as in ``test_cli_promote.py``); the policy engine, the registry and the
audit log are the production code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.commands.policy_cmd import (  # noqa: E402
    SIMULATE_EXIT_ALLOW,
    SIMULATE_EXIT_DENY,
    SIMULATE_EXIT_REQUIRE_APPROVAL,
)
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()

_ALIAS_DATA = {"registered_model": {"aliases": [{"alias": "Staging", "version": "19"}]}}
_VER_DATA = {"model_version": {"run_id": "run-abc", "version": "19"}}
_RUN_DATA = {"run": {"data": {"metrics": {"rmse": 4.5}, "params": {}, "tags": []}}}


def _get(url, **_):
    if "registered-models/get" in url:
        return _ALIAS_DATA
    if "model-versions/get" in url:
        return _VER_DATA
    if "runs/get" in url:
        return _RUN_DATA
    return {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    from examlops import platform_db

    platform_db.init_db()
    return tmp_path


def _policy(tmp_path, monkeypatch, text: str | None):
    """Point ``examlops.policy`` at a real policy file (or at a path that does not exist)."""
    path = tmp_path / "policy.yaml"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr("examlops.policy.POLICY_YAML", path)
    return path


def _promote(*extra: str, answer: str | None = None):
    args = [*extra, "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"]
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, args, input=answer)
    return res, post


def _events(action_prefix: str):
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if str(e["action"]).startswith(action_prefix)]


# ── manual promote ──────────────────────────────────────────────────────────────────────────
def test_promote_default_unchanged_and_writes_no_policy_row(env, monkeypatch):
    """No policy file: same exit code, same output, alias moved, no ``policy:*`` audit row."""
    _policy(env, monkeypatch, None)
    res, post = _promote("--yes")
    assert res.exit_code == 0, res.output
    assert "promoted jpcp v19" in res.output.lower()
    assert post.called
    assert _events("policy:") == []
    assert len(_events("alias_promoted")) == 1


def test_promote_non_matching_rule_is_also_default(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n  - action: manual_promote\n    when: \"model == 'OTHER'\"\n    effect: deny\n",
    )
    res, post = _promote("--yes")
    assert res.exit_code == 0, res.output
    assert post.called
    assert _events("policy:") == []


def test_promote_deny_blocks_with_exit_1_names_rule_and_audits(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n  - name: freeze-prod\n    action: manual_promote\n"
        "    when: \"to_alias == 'Production'\"\n    effect: deny\n",
    )
    res, post = _promote("--yes")
    assert res.exit_code == 1, res.output
    assert "freeze-prod" in res.output
    assert not post.called  # the alias never moved
    assert _events("alias_promoted") == []
    rows = _events("policy:manual_promote")
    assert len(rows) == 1
    assert json.loads(rows[0]["details"])["effect"] == "deny"
    assert json.loads(rows[0]["details"])["rule"] == "freeze-prod"


def test_promote_deny_is_not_overridable_by_force(env, monkeypatch):
    _policy(env, monkeypatch, "policies:\n  - action: manual_promote\n    effect: deny\n")
    args = ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0", "--force"]
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, args)
    assert res.exit_code == 1
    assert not post.called


def test_promote_require_approval_defaults_to_no_and_can_be_confirmed(env, monkeypatch):
    _policy(
        env, monkeypatch, "policies:\n  - action: manual_promote\n    effect: require_approval\n"
    )
    # A human at the prompt pressing Enter takes the default, which is now "no".
    res, post = _promote(answer="\n")
    assert res.exit_code == 0, res.output
    assert "policy requires approval" in res.output
    assert not post.called
    assert _events("alias_promoted") == []
    # ... and answering yes is the approval.
    res, post = _promote(answer="y\n")
    assert res.exit_code == 0, res.output
    assert post.called
    assert len(_events("policy:manual_promote")) == 2  # both decisions audited


def test_promote_dry_run_does_not_consult_policy(env, monkeypatch):
    _policy(env, monkeypatch, "policies:\n  - action: manual_promote\n    effect: deny\n")
    args = ["pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0", "--dry-run"]
    with patch("examlops.cli.commands.pipeline._client.get", side_effect=_get):
        res = runner.invoke(app, args)
    assert res.exit_code == 0
    assert _events("policy:") == []


def test_promote_condition_sees_metric_context(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n  - name: too-good\n    action: manual_promote\n"
        "    when: \"rmse_new < 5 and version == '19'\"\n    effect: deny\n",
    )
    res, _ = _promote("--yes")
    assert res.exit_code == 1
    assert "too-good" in res.output


# ── cluster approve ─────────────────────────────────────────────────────────────────────────
def _register(name="lxp"):
    from examlops import hpc_registry

    hpc_registry.register_pending(
        name,
        "flux",
        transport="ssh",
        host="lxp-login",
        ssh_user="u",
        ssh_port=22,
        ssh_key="~/.ssh/id",
        capabilities={"scheduler": "flux", "total_gpus": 8},
        requested_by="tester",
    )


def _state(name="lxp") -> str:
    from examlops.data.hpc import get_clusters

    return next(c["state"] for c in get_clusters() if c["name"] == name)


def test_approve_default_unchanged(env, monkeypatch):
    _policy(env, monkeypatch, None)
    _register()
    res = runner.invoke(app, ["--yes", "hpc", "approve", "lxp"])
    assert res.exit_code == 0, res.output
    assert "now ACTIVE" in res.output
    assert _state() == "ACTIVE"
    assert _events("policy:") == []
    assert len(_events("cluster_approved")) == 1


def test_approve_deny_blocks_and_leaves_cluster_pending(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n  - name: two-person\n    action: cluster_approve\n"
        "    when: \"scheduler == 'flux'\"\n    effect: deny\n",
    )
    _register()
    res = runner.invoke(app, ["--yes", "hpc", "approve", "lxp"])
    assert res.exit_code == 1, res.output
    assert "two-person" in res.output
    assert _state() == "PENDING"
    assert _events("cluster_approved") == []
    assert len(_events("policy:cluster_approve")) == 1


def test_approve_require_approval_prompt_defaults_to_no(env, monkeypatch):
    _policy(
        env, monkeypatch, "policies:\n  - action: cluster_approve\n    effect: require_approval\n"
    )
    _register()
    res = runner.invoke(app, ["hpc", "approve", "lxp"], input="\n")
    assert res.exit_code == 0, res.output
    assert "policy requires approval" in res.output
    assert _state() == "PENDING"
    res = runner.invoke(app, ["hpc", "approve", "lxp"], input="y\n")
    assert res.exit_code == 0, res.output
    assert _state() == "ACTIVE"


def test_engine_failure_denies_and_audits(env, monkeypatch):
    """A bug in the engine must not grant an approval (decide_safe -> deny)."""
    _policy(env, monkeypatch, None)
    _register()

    def boom(*a, **k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr("examlops.policy.decide", boom)
    res = runner.invoke(app, ["--yes", "hpc", "approve", "lxp"])
    assert res.exit_code == 1
    assert _state() == "PENDING"
    assert len(_events("policy_unavailable:cluster_approve")) == 1


# ── exa policy simulate ─────────────────────────────────────────────────────────────────────
_RULES = (
    "policies:\n"
    "  - name: no-prod\n    action: manual_promote\n    when: \"to_alias == 'Production'\"\n"
    "    effect: deny\n"
    "  - name: needs-human\n    action: manual_promote\n    when: \"to_alias == 'Canary'\"\n"
    "    effect: require_approval\n"
)


def test_simulate_exit_codes_and_no_side_effects(env, monkeypatch):
    _policy(env, monkeypatch, _RULES)
    before = len(_events(""))
    cases = [
        (["to_alias=Staging"], SIMULATE_EXIT_ALLOW, "allow"),
        (["to_alias=Production"], SIMULATE_EXIT_DENY, "deny"),
        (["to_alias=Canary"], SIMULATE_EXIT_REQUIRE_APPROVAL, "require_approval"),
    ]
    assert len({c[1] for c in cases}) == 3
    for sets, code, effect in cases:
        args = ["--json", "policy", "simulate", "manual_promote"]
        for s in sets:
            args += ["--set", s]
        res = runner.invoke(app, args)
        assert res.exit_code == code, res.output
        out = json.loads(res.stdout)
        assert out["effect"] == effect and out["exit_code"] == code
    assert len(_events("")) == before  # a simulation is never audited


def test_simulate_context_json_and_rule_named(env, monkeypatch):
    _policy(env, monkeypatch, _RULES)
    res = runner.invoke(
        app,
        [
            "--json",
            "policy",
            "simulate",
            "manual_promote",
            "--context-json",
            '{"to_alias": "Production"}',
        ],
    )
    assert res.exit_code == 1
    assert json.loads(res.stdout)["rule"] == "no-prod"


def test_simulate_bad_context_json_is_usage_error(env, monkeypatch):
    _policy(env, monkeypatch, None)
    res = runner.invoke(app, ["policy", "simulate", "x", "--context-json", "{nope"])
    assert res.exit_code == 2


def test_simulate_human_output(env, monkeypatch):
    _policy(env, monkeypatch, _RULES)
    res = runner.invoke(app, ["policy", "simulate", "manual_promote", "--set", "to_alias=Canary"])
    assert res.exit_code == 4
    assert "require_approval" in res.output and "needs-human" in res.output


# ── hpc place --explain ─────────────────────────────────────────────────────────────────────
def test_place_explain_prints_provider_and_ranked_breakdown(env, monkeypatch):
    from examlops import hpc_registry

    _register("big")
    _register("small")
    hpc_registry_caps = {
        "big": {"scheduler": "flux", "total_gpus": 16, "total_nodes": 4},
        "small": {"scheduler": "flux", "total_gpus": 4, "total_nodes": 1},
    }
    from examlops.data.hpc import set_cluster_state

    for n in ("big", "small"):
        set_cluster_state(n, "ACTIVE", approved_by="t")
    monkeypatch.setattr(
        hpc_registry,
        "active_clusters_with_inventory",
        lambda: [
            {"name": n, "scheduler": "flux", "capabilities": c, "nodes": []}
            for n, c in hpc_registry_caps.items()
        ],
    )
    plain = runner.invoke(app, ["hpc", "place", "--gpus", "2"])
    assert plain.exit_code == 0 and "Scoring provider" not in plain.output

    res = runner.invoke(app, ["--json", "hpc", "place", "--gpus", "2", "--explain"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    ex = out["explain"]
    assert ex["provider"] == "least-loaded"
    assert ex["ask"] == {"gpus": 2, "cpus": 0, "nodes": 1}
    assert ex["chosen"] == "big" == out["cluster"]
    assert [c["cluster"] for c in ex["candidates"]] == ["big", "small"]
    assert ex["candidates"][0]["margin_to_best"] == 0
    assert ex["candidates"][1]["margin_to_best"] < 0
    assert "carbon" in ex["objectives_unavailable"]
    # The non-explain JSON is unchanged (no "explain" key).
    plain_json = json.loads(runner.invoke(app, ["--json", "hpc", "place", "--gpus", "2"]).stdout)
    assert "explain" not in plain_json

    text = runner.invoke(app, ["hpc", "place", "--gpus", "2", "--explain"])
    assert "Scoring provider: least-loaded" in text.output
    assert "#1 big" in text.output and "#2 small" in text.output

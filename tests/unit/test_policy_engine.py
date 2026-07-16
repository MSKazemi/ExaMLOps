"""D5 — policy-as-code governance (OPA/Rego seam) (ADR 0029)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "test-key")
    from examlops import platform_db, policy

    # POLICY_YAML is bound at import from Path.home(); redirect it to an isolated file so no
    # real ~/.config/examlops/policy.yaml (or a sibling test's) bleeds in.
    cfg = tmp_path / "home" / ".config" / "examlops"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(policy, "POLICY_YAML", cfg / "policy.yaml")
    platform_db.init_db()
    yield


def _policy_path() -> Path:
    from examlops import policy

    return Path(policy.POLICY_YAML)


def _write_policy(text: str):
    _policy_path().write_text(text)


def test_default_engine_is_yaml():
    from examlops.policy_engine import YamlPolicyEngine, get_engine

    assert isinstance(get_engine(), YamlPolicyEngine)


def test_gwt1_promotion_denied_by_policy():
    from examlops.policy_engine import PolicyInput, evaluate

    _write_policy("policies:\n  - action: promote\n    effect: deny\n    name: no-promote\n")
    d = evaluate("promotion", PolicyInput("promote", resource="JPCP"))
    assert d.allow is False
    assert d.effect == "deny"


def test_default_allow_without_policy():
    from examlops.policy_engine import PolicyInput, evaluate

    d = evaluate("promotion", PolicyInput("promote", resource="JPCP"))
    assert d.allow is True


def test_gwt2_require_approval_effect():
    from examlops.policy_engine import PolicyInput, evaluate

    _write_policy("policies:\n  - action: promote\n    effect: require_approval\n    name: gate\n")
    d = evaluate("promotion", PolicyInput("promote"))
    assert d.requires_approval is True


def test_gwt4_supply_chain_unsigned_denied():
    from examlops.policy_engine import supply_chain_gate

    d = supply_chain_gate("JPCP", "17", signed=False)
    assert d.allow is False
    assert "unsigned" in d.reasons[0]


def test_supply_chain_signed_allowed():
    from examlops.policy_engine import supply_chain_gate

    d = supply_chain_gate("JPCP", "17", signed=True)
    assert d.allow is True


def test_gwt3_budget_over_denied():
    from examlops.policy_engine import budget_gate

    over = budget_gate(100.0, 50.0)
    assert over.allow is False
    under = budget_gate(10.0, 50.0)
    assert under.allow is True


def test_card_gate_below_floor_denied():
    from examlops.policy_engine import card_gate

    assert card_gate("JPCP", 0.5, floor=0.8).allow is False
    assert card_gate("JPCP", 0.9, floor=0.8).allow is True


def test_r4_fail_closed_on_engine_error(monkeypatch):
    import examlops.policy_engine as pe
    from examlops.policy_engine import PolicyInput, evaluate

    class Boom:
        name = "boom"

        def evaluate(self, decision, input):  # noqa: A002
            raise RuntimeError("engine down")

    monkeypatch.setattr(pe, "get_engine", lambda: Boom())
    # supply_chain is a fail-closed decision → deny on error.
    closed = evaluate("supply_chain", PolicyInput("deploy"))
    assert closed.allow is False
    # promotion is not fail-closed → monitor (allow) on error.
    monitored = evaluate("promotion", PolicyInput("promote"))
    assert monitored.allow is True


def test_gwt6_decision_audited():
    from examlops.platform_db import get_db
    from examlops.policy_engine import PolicyInput, evaluate

    evaluate("promotion", PolicyInput("promote", subject="alice", resource="JPCP", tenant="acme"))
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM audit_events WHERE action='policy_promotion'").fetchall()
    assert len(rows) == 1
    assert rows[0]["tenant"] == "acme"


def test_bundle_sign_and_verify():
    from examlops.policy_engine import sign_bundle, verify_bundle

    _write_policy("policies:\n  - action: promote\n    effect: allow\n")
    result = sign_bundle("default")
    assert result["signed"] is True
    v = verify_bundle("default")
    assert v["valid"] is True


def test_bundle_verify_detects_tamper():
    from examlops import platform_db
    from examlops.policy_engine import sign_bundle, verify_bundle

    _write_policy("policies: []\n")
    sign_bundle("default")
    # Tamper with the stored content.
    with platform_db.get_db() as conn:
        conn.execute("UPDATE policy_bundles SET content='HACKED' WHERE tenant='default'")
    v = verify_bundle("default")
    assert v["valid"] is False
    assert any("hash mismatch" in r for r in v["reasons"])


def test_tenant_overlay_in_bundle():
    from examlops.policy_engine import sign_bundle

    _write_policy("policies:\n  - action: promote\n    effect: allow\n")
    overlay = _policy_path().with_name("policy.acme.yaml")
    overlay.write_text("policies:\n  - action: promote\n    effect: deny\n")
    sign_bundle("acme")
    from examlops.platform_db import get_policy_bundle

    content = get_policy_bundle("acme")["content"]
    assert "tenant overlay: acme" in content


def test_cli_eval_and_bundle():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    _write_policy("policies:\n  - action: promote\n    effect: deny\n")
    r = runner.invoke(
        app, ["policy", "eval", "promotion", "--action", "promote", "--resource", "JPCP"]
    )
    assert r.exit_code == 0, r.output
    assert "deny" in r.output.lower()
    r = runner.invoke(app, ["policy", "bundle", "sign"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["policy", "bundle", "verify"])
    assert r.exit_code == 0, r.output

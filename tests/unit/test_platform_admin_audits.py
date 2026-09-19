"""Governance guard for ``examlops.platform_admin`` (M1).

Asserts the platform-management façade closes the audit gap: every write goes through
RBAC → policy → audit, so a notebook/dashboard change is attributable and tamper-evident
exactly like a CLI change. Also proves policy ``deny`` / ``require_approval`` block the write
*before* it touches a store, and that secret/token values never reach the audit trail.
"""

from __future__ import annotations

import pytest
import yaml

import examlops.platform_admin as pa
from examlops.data.audit import verify_audit_chain
from examlops.platform_db import get_db, init_db

# A valid Fernet key so connection-secret writes work under the D7 secrets store.
_FERNET = "3jZ8n4bQ6h3nJh5m3nJh5m3nJh5m3nJh5m3nJh5m3nI="


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", _FERNET)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", _FERNET)
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY", raising=False)
    # Isolate the file-backed config surfaces (module constants are computed from Path.home()).
    import examlops.policy as policy
    import examlops.providers.loader as loader

    monkeypatch.setattr(loader, "FINOPS_YAML", tmp_path / "finops.yaml")
    monkeypatch.setattr(policy, "POLICY_YAML", tmp_path / "policy.yaml")
    monkeypatch.setattr(pa, "_finops_yaml_path", lambda: tmp_path / "finops.yaml")
    init_db()
    yield tmp_path


def _audit_rows(action_prefix: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action LIKE ? ORDER BY id", (f"{action_prefix}%",)
        ).fetchall()
    return [dict(r) for r in rows]


def _write_policy(tmp_path, rules: list[dict]) -> None:
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump({"policies": rules}))


# ── the audit gap is closed: every write emits an attributed, chained audit row ──


def test_set_compute_cost_writes_finops_and_audits(_env):
    out = pa.set_compute_cost(gpu_per_hour=3.10, cpu_per_hour=0.07)
    assert out["ok"] and out["actor"] == "tester"

    data = yaml.safe_load((_env / "finops.yaml").read_text())
    assert data["finops"]["cost"]["coefficients"] == {"gpu_rate": 3.10, "cpu_rate": 0.07}

    card = pa.compute_cost_card()
    assert card["gpu_rate"] == 3.10 and card["cost_per_gpu_hour"] == 3.10

    rows = _audit_rows("platform_admin:set_compute_cost")
    assert len(rows) == 1
    assert rows[0]["actor"] == "tester" and rows[0]["source"] == "workbench"
    assert verify_audit_chain()["ok"] is True


def test_deploy_provider_gated_and_audited(_env):
    code = (
        "class MyCost(Provider):\n"
        "    name = 'nb-cost'\n"
        "    def compute(self, inputs):\n"
        "        return {'cost_usd': inputs.get('gpu_hours', 0) * 0.9}\n"
    )
    out = pa.deploy_provider("cost", "nb-cost", code, project="platform-ops")
    assert out["result"]["activated"] is True
    assert any(
        p["name"] == "nb-cost" and p["active"] for p in pa.list_authored_providers("platform-ops")
    )
    assert len(_audit_rows("platform_admin:deploy_provider:cost")) == 1


def test_deploy_provider_rejects_unsafe_source_before_disk(_env):
    # AST gate (providers/sandbox) must reject an import — nothing lands on disk, nothing audited.
    with pytest.raises(Exception):
        pa.deploy_provider("cost", "evil", "import os\nclass X(Provider):\n    name='x'\n")
    assert pa.list_authored_providers("platform-ops") == []


def test_set_connection_never_audits_secret(_env):
    pa.set_connection(
        "dp", "dataplane", config={"endpoint": "http://dp:8000"}, secret_value="topsecret"
    )
    rows = _audit_rows("platform_admin:set_connection")
    assert len(rows) == 1
    assert rows[0]["details"] is not None
    assert "topsecret" not in rows[0]["details"]
    assert '"has_secret": true' in rows[0]["details"]


def test_set_config_redacts_token_in_audit(_env):
    pa.set_config(control_plane_token="s3cr3t", control_plane_url="http://cp:18002")
    rows = _audit_rows("platform_admin:set_config")
    assert "s3cr3t" not in rows[0]["details"] and "***" in rows[0]["details"]


def test_set_knob_traffic_routes_through_data_setter(_env):
    pa.set_knob("traffic", "JPCP", {"Production": 90, "Canary": 10})
    from examlops.data.serving import get_traffic_rules

    assert get_traffic_rules("JPCP") == {"Production": 90, "Canary": 10}
    assert len(_audit_rows("platform_admin:set_knob:traffic")) == 1


def test_set_knob_unknown_domain_raises(_env):
    with pytest.raises(pa.PlatformAdminError):
        pa.set_knob("nope", "JPCP", {})


def test_set_bridge_uuid_edits_yaml_and_audits(_env, monkeypatch):
    import examlops.cli.commands.seanerbus_cmd as sb

    models = _env / "models"
    models.mkdir()
    (models / "jpcp.yaml").write_text("name: JPCP\nenabled: true\n")
    monkeypatch.setattr(sb, "MODELS_DIR", models)

    out = pa.set_bridge_uuid("JPCP")
    assert out["result"]["changed"] is True and out["result"]["uuid"]
    assigned = yaml.safe_load((models / "jpcp.yaml").read_text())["seanerbus_uuid"]
    # idempotent: a second non-regen call does not change it
    out2 = pa.set_bridge_uuid("JPCP")
    assert out2["result"]["changed"] is False and out2["result"]["uuid"] == assigned
    assert len(_audit_rows("platform_admin:set_bridge_uuid")) == 2


# ── policy gates block the write before it touches a store ──


def test_policy_deny_blocks_before_write(_env, tmp_path):
    _write_policy(tmp_path, [{"action": "platform_admin:set_compute_cost", "effect": "deny"}])
    with pytest.raises(pa.PlatformAdminDenied):
        pa.set_compute_cost(gpu_per_hour=9.99)
    assert not (tmp_path / "finops.yaml").exists()  # nothing written


def test_a_broken_policy_engine_denies_instead_of_crashing(_env, monkeypatch):
    """BL-080: this call site used to have no try/except around `decide` at all, so a bug in the
    policy engine crashed the caller with a bare traceback — accidentally safe (nothing then
    wrote), but with no audit row and no clean error. `decide_safe` fails closed the same as the
    MCP write-gate (a notebook/dashboard call has no human confirming a require_approval prompt,
    same reasoning as autopilot) and is itself durably audited."""
    import examlops.policy as policy

    def boom(*a, **k):
        raise RuntimeError("policy exploded")

    monkeypatch.setattr(policy, "decide", boom)
    with pytest.raises(pa.PlatformAdminDenied, match="policy exploded"):
        pa.set_compute_cost(gpu_per_hour=9.99)

    rows = _audit_rows("policy_unavailable:platform_admin:set_compute_cost")
    assert rows, "no audit_events row recorded when the policy engine raised"
    assert "policy exploded" in rows[0]["details"]


def test_policy_require_approval_then_approve(_env, tmp_path):
    _write_policy(
        tmp_path, [{"action": "platform_admin:set_compute_cost", "effect": "require_approval"}]
    )
    with pytest.raises(pa.PlatformAdminApprovalRequired):
        pa.set_compute_cost(gpu_per_hour=4.0)
    assert not (tmp_path / "finops.yaml").exists()

    out = pa.set_compute_cost(gpu_per_hour=4.0, approve=True)
    assert out["ok"]
    rows = _audit_rows("platform_admin:set_compute_cost")
    assert any("approved_by" in (r["details"] or "") for r in rows)


# ── Tier B + change feed ──


def test_propose_source_change_records_intent_only(_env):
    out = pa.propose_source_change(["platform/clients/seanerbus_bridge.py"], "tune retry")
    assert out["result"]["next_steps"] and "dualgit" in " ".join(out["result"]["next_steps"])
    assert len(_audit_rows("platform_admin:platform_source_change")) == 1


def test_recent_changes_feed_newest_first(_env):
    pa.set_compute_cost(gpu_per_hour=2.0)
    pa.set_knob("traffic", "JPCP", {"Production": 100})
    feed = pa.recent_changes(limit=10)
    assert feed[0]["action"] == "platform_admin:set_knob:traffic"  # newest first
    assert all(r["source"] == "workbench" for r in feed)

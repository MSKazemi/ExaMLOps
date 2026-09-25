"""Every mutating MCP tool obeys one contract, and the interesting half is the failure case.

Three of the nine (`hpc_approve_cluster`, `project_assign_model`, `project_add_member`) used to
wrap the audit write inside the same ``try`` as the action, so a broken audit chain produced
``{"ok": False}`` *after the action had already taken effect* — measured, not theorised: a cluster
left ``ACTIVE`` (jobs schedulable on it) while the caller was told the approval failed, and no audit
row to show it ever happened. An operator reading that reply retries, or believes nothing changed.

The contract these tests pin:

1. a mutating tool that succeeds leaves an audit event;
2. if the audit cannot be written, the tool still reports ``ok: True`` — the action happened, and
   saying otherwise is false;
3. …but never silently: it carries an ``audit_warning``, because an unaudited governance write is
   exactly the thing someone needs to be told about.

(2) and (3) only make sense together. Either alone is a defect: (2) alone hides the governance gap,
(3) alone is the bug this file was written for.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    monkeypatch.setenv("EXAMLOPS_ACTOR", "test-actor")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "test-admin-token-for-audit-contract")
    # A fully local success path for dataplane_pull (ADR 0130): the built-in `files` connector
    # reading a tiny parquet drop through a `file://` store — same recipe as
    # tests/unit/test_dataplane_files.py / test_dataplane_pull.py, no live infrastructure needed.
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'dpstore'}")
    # A fully local success path for genai_app_invoke (ADR 0159 §3 Phase 3): the gateway's own
    # always-present echo route, no RAG binding, a real prompt version and a real, secret-stored
    # virtual key — no live LLM or gateway service needed, the same reason this tool needed no
    # `_reload_gateway_with_a_mocked_service`-style stub above it.
    monkeypatch.delenv("EXAMLOPS_SECRETS_KEYS", raising=False)
    monkeypatch.delenv("EXAMLOPS_SECRETS_ACTIVE_KEY", raising=False)
    monkeypatch.delenv("EXAMLOPS_GATEWAY_CONFIG", raising=False)
    monkeypatch.setattr("examlops.gateway.config.default_config_path", lambda: None, raising=True)

    from cryptography.fernet import Fernet

    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())

    from examlops import dataplane as dpl
    from examlops import genai_apps as ga
    from examlops.data import init_db
    from examlops.data.projects import create_project
    from examlops.data.prompts import create_prompt_version, set_prompt_label
    from examlops.gateway import issue_virtual_key
    from examlops.hpc_registry import register_pending
    from examlops.secrets import set_secret

    init_db()
    create_project("proj")
    register_pending("cl", "mock", host="localhost")

    v = create_prompt_version(
        "ga-prompt", "Answer using {context}.", variables=["context"], actor="t"
    )
    set_prompt_label("ga-prompt", "prod", v)
    raw_key = issue_virtual_key("default", "default", None, None, "t", source="test")
    set_secret("gateway/ga-app", raw_key, tenant="default", actor="t")
    ga_vid = ga.register(
        {
            "schema_version": 1,
            "name": "ga-app",
            "route": {"model": "default", "key_ref": "gateway/ga-app"},
            "prompt": {"name": "ga-prompt", "label": "prod"},
            "guardrail": {"mode": "enforce", "policy": "default"},
        }
    )["version_id"]
    # Staging, not Production: Production is gated on evaluation evidence (ADR 0159 decision 3),
    # ceremony this fixture has no reason to carry — `set_alias` to any *other* alias is ungated.
    ga.set_alias("ga-app", "Staging", ga_vid)

    drop = tmp_path / "drop"
    drop.mkdir()
    pq.write_table(pa.Table.from_pylist([{"a": 1}, {"a": 2}]), drop / "data.parquet")
    dpl.define_source(
        "filesrc", "files", spec={"url": f"file://{drop}", "glob": "*.parquet"}, actor="t"
    )
    yield


def _reload_gateway_with_a_mocked_service():
    """`gateway_service_reload` reaches a real network service; stub the one HTTP call it makes
    (the same seam `test_mcp_gateway_service_tools.py` mocks) so it is a `_cases()` entry like
    every other tool here, not one more networked exemption alongside `trigger_retrain`."""
    from examlops.mcp import tools as T

    with patch.object(T._client, "post", return_value={"reloaded": True, "routes": ["chat"]}):
        return T.gateway_service_reload()


def _cases():
    """(tool name, call) for every mutating tool that runs without a live service."""
    from examlops.mcp import tools as T

    return [
        ("gateway_service_reload", _reload_gateway_with_a_mocked_service),
        ("set_traffic_split", lambda: T.set_traffic_split("JPCP", 100, 0)),
        ("disable_challenger", lambda: T.disable_challenger("JPCP")),
        ("set_drift_autoretrain", lambda: T.set_drift_autoretrain("JPCP", "PM100Dataset", True)),
        ("set_promotion_rule", lambda: T.set_promotion_rule("JPCP", "rmse", "<", 5.0)),
        ("grant_access", lambda: T.grant_access("alice", "viewer", "project:proj")),
        ("project_assign_model", lambda: T.project_assign_model("proj", "JPCP")),
        ("project_add_member", lambda: T.project_add_member("proj", "bob", "editor")),
        ("hpc_approve_cluster", lambda: T.hpc_approve_cluster("cl")),
        ("dataplane_pull", lambda: T.dataplane_pull("filesrc")),
        ("genai_app_invoke", lambda: T.genai_app_invoke("ga-app@Staging", "hello")),
    ]


def test_the_offline_cases_cover_every_mutating_tool_but_the_networked_one():
    """If a mutating tool is added and not listed here, this file stops being a contract."""
    from examlops.mcp.tools import REGISTRY
    from examlops.plans import PLAN_TOOLS

    # plan/apply/approve delegate to the tools' own gates + audits: tests/unit/test_plan_apply.py.
    mutating = {t.name for t in REGISTRY if t.mutating and t.name not in PLAN_TOOLS}
    covered = {name for name, _ in _cases()}
    # trigger_retrain / operation_cancel need a live control plane; they are exercised separately
    # (tests/unit/test_operations.py runs operation_cancel against the real control-plane app).
    assert mutating - covered == {"trigger_retrain", "operation_cancel"}, mutating - covered


@pytest.mark.parametrize("name", [c[0] for c in _cases()])
def test_a_successful_write_is_audited(platform, name):
    from examlops.data.audit import export_audit_events

    call = dict(_cases())[name]
    before = len(export_audit_events())
    result = call()
    assert result.get("ok") is True, result
    assert "audit_warning" not in result, result
    assert len(export_audit_events()) > before, f"{name} wrote nothing to the audit log"


@pytest.mark.parametrize("name", [c[0] for c in _cases()])
def test_an_unwritable_audit_log_does_not_turn_a_done_action_into_an_error(platform, name):
    call = dict(_cases())[name]

    def boom(*_a, **_k):
        raise RuntimeError("audit chain unavailable")

    with patch("examlops.data.audit.write_audit_event", boom):
        result = call()

    assert result.get("ok") is True, (
        f"{name} reported failure for an action that already happened: {result}"
    )
    assert "audit_warning" in result, (
        f"{name} performed an unaudited write and said nothing about it: {result}"
    )
    assert "not audited" in result["audit_warning"]


def test_the_cluster_really_is_active_when_the_audit_failed(platform):
    """The concrete case, spelled out: the state change is real either way."""
    from examlops.data.hpc import get_cluster
    from examlops.mcp import tools as T

    def boom(*_a, **_k):
        raise RuntimeError("audit chain unavailable")

    with patch("examlops.data.audit.write_audit_event", boom):
        result = T.hpc_approve_cluster("cl")

    assert result["ok"] is True and "audit_warning" in result
    assert get_cluster("cl")["state"] == "ACTIVE"


def test_writes_are_not_exposed_at_all_unless_enabled(monkeypatch):
    """The coarse switch is the first layer: with writes off, nothing mutating is offered."""
    from examlops.mcp.tools import REGISTRY, iter_tools

    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    assert [t.name for t in iter_tools() if t.mutating] == []
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    assert len([t for t in iter_tools() if t.mutating]) == len([t for t in REGISTRY if t.mutating])

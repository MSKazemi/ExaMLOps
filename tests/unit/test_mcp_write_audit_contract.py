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

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    monkeypatch.setenv("EXAMLOPS_ACTOR", "test-actor")

    from examlops.data import init_db
    from examlops.data.projects import create_project
    from examlops.hpc_registry import register_pending

    init_db()
    create_project("proj")
    register_pending("cl", "mock", host="localhost")
    yield


def _cases():
    """(tool name, call) for every mutating tool that runs without a live service."""
    from examlops.mcp import tools as T

    return [
        ("set_traffic_split", lambda: T.set_traffic_split("JPCP", 100, 0)),
        ("disable_challenger", lambda: T.disable_challenger("JPCP")),
        ("set_drift_autoretrain", lambda: T.set_drift_autoretrain("JPCP", "PM100Dataset", True)),
        ("set_promotion_rule", lambda: T.set_promotion_rule("JPCP", "rmse", "<", 5.0)),
        ("grant_access", lambda: T.grant_access("alice", "viewer", "project:proj")),
        ("project_assign_model", lambda: T.project_assign_model("proj", "JPCP")),
        ("project_add_member", lambda: T.project_add_member("proj", "bob", "editor")),
        ("hpc_approve_cluster", lambda: T.hpc_approve_cluster("cl")),
    ]


def test_the_offline_cases_cover_every_mutating_tool_but_the_networked_one():
    """If a mutating tool is added and not listed here, this file stops being a contract."""
    from examlops.mcp.tools import REGISTRY

    mutating = {t.name for t in REGISTRY if t.mutating}
    covered = {name for name, _ in _cases()}
    # trigger_retrain is the one that needs a live control plane; it is exercised separately.
    assert mutating - covered == {"trigger_retrain"}, mutating - covered


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

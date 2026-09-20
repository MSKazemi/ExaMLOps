"""ADR 0014 decision 4: a denied subject is refused at the ``exa project`` paths, end to end.

Real code throughout: the CLI app, the real ``authz`` relation table, the real audit log. The
subject is the CLI actor (``EXAMLOPS_ACTOR``); switching it switches who is asking.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops import authz
from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY", raising=False)
    monkeypatch.delenv("EXAMLOPS_AUTHZ_ADMINS", raising=False)
    monkeypatch.delenv("EXAMLOPS_OPENFGA_URL", raising=False)
    from examlops.platform_db import init_db

    init_db()
    return monkeypatch


def _as(env, who: str, *args: str):
    env.setenv("EXAMLOPS_ACTOR", who)
    return runner.invoke(app, ["--yes", *args])


def _quota(name: str = "acme") -> float:
    from examlops.data.projects import get_project

    return get_project(name)["cpu_limit"]


def _denials() -> list[dict]:
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if e["action"] == "authz_deny"]


# --- flag off: nothing changes (ADR 0014 decision 5) ------------------------------------------


def test_flag_off_everyone_may_everything(env):
    assert _as(env, "alice", "project", "create", "acme").exit_code == 0
    assert _as(env, "mallory", "project", "set-quota", "acme", "--cpu-limit", "9").exit_code == 0
    assert _quota() == 9
    assert (
        _as(env, "mallory", "project", "add-member", "acme", "eve", "--role", "owner").exit_code
        == 0
    )
    assert _as(env, "mallory", "project", "delete", "acme").exit_code == 0


# --- flag on ----------------------------------------------------------------------------------


@pytest.fixture
def tenant(env):
    env.setenv("EXAMLOPS_MULTITENANCY", "1")
    assert _as(env, "alice", "project", "create", "acme").exit_code == 0
    return env


def test_creator_becomes_owner(tenant):
    assert authz.check("alice", "owner", "project:acme")
    assert not authz.check("mallory", "viewer", "project:acme")


@pytest.mark.parametrize(
    "args",
    [
        ("project", "set-quota", "acme", "--cpu-limit", "99"),
        ("project", "assign", "acme", "JPCP", "--kind", "model"),
        ("project", "assign-model", "acme", "JPCP"),
        ("project", "add-member", "acme", "mallory", "--role", "owner"),
        ("project", "remove-member", "acme", "alice"),
        ("project", "storage", "acme"),
        ("project", "archive", "acme"),
        ("project", "delete", "acme"),
        ("project", "show", "acme"),
        ("project", "members", "acme"),
        ("project", "pipelines", "acme"),
        ("project", "cost", "acme"),
        ("project", "budget", "acme"),
        ("project", "compose", "acme"),
        ("project", "grant", "mallory", "owner", "project:acme"),
        ("project", "grant", "mallory", "viewer", "project:acme/model:JPCP"),
        ("project", "revoke", "alice", "owner", "project:acme"),
    ],
)
def test_denied_subject_is_refused_with_exit_1_and_no_effect(tenant, args):
    res = _as(tenant, "mallory", *args)
    assert res.exit_code == 1, res.output
    assert "Permission denied" in res.output
    # Nothing happened.
    assert _quota() == 4.0
    from examlops.data.projects import get_project

    assert get_project("acme")["status"] == "ACTIVE"
    assert authz.check("alice", "owner", "project:acme")
    assert not authz.check("mallory", "viewer", "project:acme")


def test_denials_are_audited(tenant):
    _as(tenant, "mallory", "project", "delete", "acme")
    denied = _denials()
    assert denied and denied[-1]["actor"] == "mallory"
    assert json.loads(denied[-1]["details"])["relation"] == "owner"


def test_role_ladder_viewer_editor_owner(tenant):
    assert (
        _as(tenant, "alice", "project", "add-member", "acme", "vic", "--role", "viewer").exit_code
        == 0
    )
    assert (
        _as(tenant, "alice", "project", "add-member", "acme", "eddie", "--role", "editor").exit_code
        == 0
    )
    # viewer: may read, may not write
    assert _as(tenant, "vic", "project", "show", "acme").exit_code == 0
    assert _as(tenant, "vic", "project", "set-quota", "acme", "--cpu-limit", "5").exit_code == 1
    # editor: may write quota, may not manage people or delete
    assert _as(tenant, "eddie", "project", "set-quota", "acme", "--cpu-limit", "5").exit_code == 0
    assert _quota() == 5
    assert (
        _as(tenant, "eddie", "project", "add-member", "acme", "x", "--role", "viewer").exit_code
        == 1
    )
    assert _as(tenant, "eddie", "project", "delete", "acme").exit_code == 1
    # owner: everything
    assert (
        _as(tenant, "alice", "project", "add-member", "acme", "y", "--role", "viewer").exit_code
        == 0
    )


def test_a_denied_subject_learns_nothing_about_existence(tenant):
    """A stranger gets the same refusal for a real and an unknown project (no 404 oracle)."""
    real = _as(tenant, "mallory", "project", "show", "acme")
    ghost = _as(tenant, "mallory", "project", "show", "does-not-exist")
    assert real.exit_code == ghost.exit_code == 1
    assert "Permission denied" in real.output and "Permission denied" in ghost.output


def test_list_shows_only_my_projects(tenant):
    _as(tenant, "bob", "project", "create", "bobs")
    tenant.setenv("EXAMLOPS_ACTOR", "alice")
    res = runner.invoke(app, ["--json", "project", "list"])
    assert [p["name"] for p in json.loads(res.stdout)] == ["acme"]


def test_bootstrap_admins_may_act_on_any_project(tenant):
    tenant.setenv("EXAMLOPS_AUTHZ_ADMINS", "root-ops")
    assert (
        _as(tenant, "root-ops", "project", "set-quota", "acme", "--cpu-limit", "7").exit_code == 0
    )
    assert _quota() == 7
    assert _as(tenant, "mallory", "project", "set-quota", "acme", "--cpu-limit", "8").exit_code == 1


def test_grant_on_non_project_object_needs_ownership(tenant):
    assert (
        _as(tenant, "mallory", "project", "grant", "mallory", "owner", "platform:core").exit_code
        == 1
    )
    tenant.setenv("EXAMLOPS_AUTHZ_ADMINS", "root-ops")
    assert (
        _as(tenant, "root-ops", "project", "grant", "carol", "editor", "platform:core").exit_code
        == 0
    )
    assert authz.check("carol", "editor", "platform:core")


def test_default_tenant_semantics_with_flag_toggled(env):
    """The same actor: refused with tenancy on, allowed the moment it is off (flag = allow-all)."""
    env.setenv("EXAMLOPS_MULTITENANCY", "1")
    assert _as(env, "alice", "project", "create", "acme").exit_code == 0
    assert _as(env, "mallory", "project", "set-quota", "acme", "--cpu-limit", "9").exit_code == 1
    env.delenv("EXAMLOPS_MULTITENANCY")
    assert _as(env, "mallory", "project", "set-quota", "acme", "--cpu-limit", "9").exit_code == 0
    assert _quota() == 9


def test_legacy_subjects_hold_only_the_default_project_migration_grants(env):
    from examlops.authz import guard

    env.setenv("EXAMLOPS_MULTITENANCY", "1")
    assert guard.allowed("legacy:admin", "owner", "default")
    assert guard.allowed("legacy:viewer", "viewer", "default")
    assert not guard.allowed("legacy:viewer", "editor", "default")
    assert not guard.allowed("legacy:admin", "owner", "acme")
    authz.grant("legacy:admin", "owner", "project:acme")
    assert guard.allowed("legacy:admin", "owner", "acme")


def test_backend_failure_denies_not_allows(tenant, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("datastore down")

    monkeypatch.setattr("examlops.data.governance.get_relations_for", boom)
    assert _as(tenant, "alice", "project", "set-quota", "acme", "--cpu-limit", "9").exit_code == 1
    assert _quota() == 4.0

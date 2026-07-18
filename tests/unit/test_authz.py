# tests/unit/test_authz.py
"""D6 — Fine-grained RBAC & multi-tenancy (ADR 0014, spec D6).

GWT-1 default deny · GWT-2 cross-project · GWT-3 role implication ·
GWT-5 flag-off single-tenant compat.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import authz  # noqa: E402
from examlops.platform_db import init_db, list_relations  # noqa: E402


@pytest.fixture(autouse=True)
def _mt(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "true")  # enable enforcement for tests
    init_db()


def test_gwt1_default_deny():
    assert authz.check("alice", "viewer", "model:JPCP") is False


def test_grant_then_allow():
    authz.grant("alice", "viewer", "model:JPCP", actor="admin")
    assert authz.check("alice", "viewer", "model:JPCP") is True


def test_gwt3_owner_implies_editor_and_viewer():
    authz.grant("alice", "owner", "model:JPCP", actor="admin")
    assert authz.check("alice", "owner", "model:JPCP")
    assert authz.check("alice", "editor", "model:JPCP")
    assert authz.check("alice", "viewer", "model:JPCP")


def test_gwt3_editor_cannot_do_owner_actions():
    authz.grant("bob", "editor", "model:JPCP", actor="admin")
    assert authz.check("bob", "editor", "model:JPCP") is True
    assert authz.check("bob", "owner", "model:JPCP") is False  # delete is owner-only


def test_project_grant_covers_children():
    authz.grant("carol", "owner", "project:acme", actor="admin")
    # a project-level owner is owner of its child objects (hierarchical inheritance)
    assert authz.check("carol", "editor", "project:acme/model:JPCP") is True
    assert authz.check("carol", "viewer", "project:acme/dataset:FData") is True


def test_gwt2_cross_project_denied():
    authz.grant("dave", "owner", "project:A", actor="admin")
    assert authz.check("dave", "viewer", "project:B/dataset:X") is False


def test_revoke_removes_access():
    authz.grant("eve", "viewer", "model:M", actor="admin")
    assert authz.check("eve", "viewer", "model:M")
    authz.revoke("eve", "viewer", "model:M", actor="admin")
    assert authz.check("eve", "viewer", "model:M") is False


def test_list_objects_filters_by_relation():
    authz.grant("frank", "viewer", "model:A", actor="admin")
    authz.grant("frank", "owner", "model:B", actor="admin")
    owners = authz.list_objects("frank", "editor")  # editor-or-stronger
    objs = {r["object"] for r in owners}
    assert objs == {"model:B"}  # only the owner grant qualifies for editor


def test_denials_audited():
    authz.check("mallory", "owner", "model:secret")
    actions = [r["relation"] for r in list_relations()]  # sanity: no grant made
    assert actions == []


def test_gwt5_flag_off_allows_everything(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "false")
    # single-tenant compat: no relations, but everything is allowed
    assert authz.check("anyone", "owner", "model:JPCP") is True


def test_require_raises_on_deny():
    with pytest.raises(PermissionError):
        authz.require("nobody", "viewer", "model:X")


def test_check_fails_closed_on_backend_error(monkeypatch):
    """0.7: a datastore failure during a check must DENY (never accidentally allow)."""

    def _boom(*_a, **_k):
        raise RuntimeError("simulated authz backend outage")

    # Break the relation read the parent-walk depends on.
    monkeypatch.setattr(authz, "_best_rank_on", _boom)
    assert authz.check("alice", "viewer", "model:JPCP") is False


def test_require_fails_closed_on_backend_error(monkeypatch):
    """0.7: enforcement points raise PermissionError when the backend errors (deny)."""

    def _boom(*_a, **_k):
        raise RuntimeError("simulated authz backend outage")

    monkeypatch.setattr(authz, "_best_rank_on", _boom)
    with pytest.raises(PermissionError):
        authz.require("alice", "viewer", "model:JPCP")

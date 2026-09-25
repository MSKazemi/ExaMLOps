"""ADR 0014 decision 4 at the control plane: model routes are authorized per project.

Real code throughout - the FastAPI app, the real ``authz_relations`` table, the real project
membership tables and the real audit log in one SQLite file. Only the Prefect dispatch worker is
switched off (as in every control-plane test). The route-table half is the same forcing function
as ``test_policy_route_table.py``: a new mutating route must be classified.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re

import pytest
from cplane.project_gate import ROUTE_PROJECT, asserted_projects, route_key, subject_of
from fastapi.testclient import TestClient

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

CREDENTIALS = {
    "bob-writer-token-value-0001": {"principal": "bob", "tenant": "default", "scopes": ["write"]},
    "eve-writer-token-value-0001": {"principal": "eve", "tenant": "default", "scopes": ["write"]},
    "ci-changes-token-value-0001": {"principal": "ci", "tenant": "default", "scopes": ["changes"]},
    "ops-writer-token-value-0001": {"principal": "ops", "tenant": "default", "scopes": ["write"]},
}
BOB, EVE, CI, OPS = ({"Authorization": f"Bearer {t}"} for t in CREDENTIALS)
LEGACY_TOKEN = "legacy-shared-token-value-000001"
LEGACY = {"Authorization": f"Bearer {LEGACY_TOKEN}"}
RETRAIN = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    db = str(tmp_path / "platform.db")
    monkeypatch.setenv("CONTROL_PLANE_CREDENTIALS_JSON", json.dumps(CREDENTIALS))
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", LEGACY_TOKEN)
    monkeypatch.setenv("CONTROL_PLANE_DB", db)
    monkeypatch.setenv("PLATFORM_DB", db)
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    for var in ("EXAMLOPS_MULTITENANCY", "EXAMLOPS_AUTHZ_ADMINS", "EXAMLOPS_OPENFGA_URL"):
        monkeypatch.delenv(var, raising=False)
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_start_command_workers", lambda: None)
    return cp_app


@pytest.fixture()
def tenancy(cp, monkeypatch):
    """Multi-tenancy on; JPCP belongs to project ``acme``, which only ``bob`` may edit."""
    from examlops.authz import grant
    from examlops.data.projects import assign_model_to_project, create_project

    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    create_project("acme")
    assert assign_model_to_project("acme", "JPCP")
    grant("bob", "editor", "project:acme", actor="test")
    return cp


def _retrain(client, headers):
    return client.post("/v1/retrain", json=RETRAIN, headers=headers)


def _denials() -> list[dict]:
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if e["action"] == "authz_deny"]


def _metric(cp, outcome: str) -> float:
    return cp._metrics.project_authz_decisions.labels(outcome=outcome)._value.get()


# --- the route table ---------------------------------------------------------------------------


def _live(cp) -> set[tuple[str, str]]:
    schema = {
        (m.upper(), path)
        for path, ops in cp.app.openapi()["paths"].items()
        for m in ops
        if m.upper() in MUTATING
    }
    walked = {
        (m, str(getattr(r, "path", "")))
        for r in cp.app.routes
        for m in (getattr(r, "methods", None) or ())
        if m in MUTATING
    }
    assert schema, "openapi() lists no mutating operation - the guard would pass vacuously"
    return {route_key(m, re.sub(r"\{(\w+):\w+\}", r"{\1}", p)) for m, p in schema | walked}


def test_every_mutating_route_is_classified(cp):
    missing = sorted(_live(cp) - set(ROUTE_PROJECT))
    assert not missing, f"classify in cplane.project_gate.ROUTE_PROJECT: {missing}"
    assert not sorted(set(ROUTE_PROJECT) - _live(cp)), "stale ROUTE_PROJECT entries"


def test_exemptions_state_a_reason(cp):
    for key, (kind, text) in ROUTE_PROJECT.items():
        assert kind in {"model", "exempt"}, key
        if kind == "exempt":
            assert len(text.split()) >= 6, key
        else:
            assert text in {"viewer", "editor", "owner"}, key


@pytest.mark.parametrize(
    "handler",
    [
        "trigger_retrain",
        "submit_retrain_v1",
        "notify_changes",
        "approve_model",
        "reject_model",
        "retract_approval",
        "cancel_command_v1",
    ],
)
def test_model_route_handlers_call_the_gate(cp, handler):
    src = inspect.getsource(getattr(cp, handler))
    assert "_project_gate.enforce_model" in src or "_enforce_row_model(" in src, handler


# --- flag off: byte-identical behaviour ---------------------------------------------------------


def test_flag_off_any_writer_may_retrain_any_model(cp):
    from examlops.data.projects import assign_model_to_project, create_project

    create_project("acme")
    assign_model_to_project("acme", "JPCP")
    assert _retrain(TestClient(cp.app), EVE).status_code == 202
    assert _denials() == []


# --- flag on ------------------------------------------------------------------------------------


def test_a_non_member_cannot_retrain_another_projects_model(tenancy):
    before = _metric(tenancy, "deny")
    r = _retrain(TestClient(tenancy.app), EVE)
    assert r.status_code == 403
    assert "project of model 'JPCP'" in r.text
    assert _metric(tenancy, "deny") == before + 1
    denial = _denials()[-1]
    assert denial["target"] == "project:acme/model:JPCP"
    assert "eve" in json.dumps(denial)
    conn = tenancy._get_db()
    try:  # nothing was claimed or queued for the denied caller
        assert conn.execute("SELECT COUNT(*) FROM control_plane_commands").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_legacy_route_is_gated_too(tenancy):
    r = TestClient(tenancy.app).post("/retrain", json=RETRAIN, headers=EVE)
    assert r.status_code == 403


def test_a_project_editor_may_retrain(tenancy):
    before = _metric(tenancy, "allow")
    r = _retrain(TestClient(tenancy.app), BOB)
    assert r.status_code == 202, r.text
    assert _metric(tenancy, "allow") == before + 1


def test_a_model_level_grant_is_enough(tenancy):
    from examlops.authz import grant

    grant("eve", "editor", "project:acme/model:JPCP", actor="test")
    assert _retrain(TestClient(tenancy.app), EVE).status_code == 202


def test_a_viewer_is_not_an_editor(tenancy):
    from examlops.authz import grant

    grant("eve", "viewer", "project:acme", actor="test")
    assert _retrain(TestClient(tenancy.app), EVE).status_code == 403


def test_approve_and_reject_are_gated_before_any_lookup(tenancy):
    client = TestClient(tenancy.app)
    # No approval exists: a denied caller gets 403, not the 404 that would confirm its absence.
    assert client.post("/approve/JPCP", headers=EVE).status_code == 403
    assert client.post("/reject/JPCP", headers=EVE).status_code == 403
    assert client.post("/v1/approvals/JPCP/approve", headers=EVE).status_code == 403
    # The editor gets through to the handler (404: nothing pending).
    assert client.post("/reject/JPCP", headers=BOB).status_code == 404


def test_ci_cannot_open_approvals_for_a_project_it_does_not_edit(tenancy):
    body = {"model_ids": ["JPCP"], "commit_sha": "abc123", "changed_files": []}
    client = TestClient(tenancy.app)
    assert client.post("/api/changes", json=body, headers=CI).status_code == 403
    conn = tenancy._get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM pending_approvals").fetchone()[0] == 0
    finally:
        conn.close()
    from examlops.authz import grant

    grant("ci", "editor", "project:acme", actor="test")
    assert client.post("/api/changes", json=body, headers=CI).status_code == 200


def test_retract_and_cancel_authorize_the_rows_model(tenancy):
    client = TestClient(tenancy.app)
    body = {"model_ids": ["JPCP"], "commit_sha": "abc123", "changed_files": []}
    assert client.post("/api/changes", json=body, headers=BOB).status_code == 200
    command = _retrain(client, BOB).json()["command_id"]
    conn = tenancy._get_db()
    try:
        approval = conn.execute("SELECT id FROM pending_approvals").fetchone()[0]
    finally:
        conn.close()

    assert client.delete(f"/approvals/{approval}", headers=EVE).status_code == 403
    assert client.delete(f"/v1/commands/{command}", headers=EVE).status_code == 403
    assert client.delete(f"/approvals/{approval}", headers=BOB).status_code == 200
    assert client.delete(f"/v1/commands/{command}", headers=BOB).status_code == 200
    # An id that does not exist names no model: the handler's own 404, as before.
    assert client.delete("/approvals/nope", headers=EVE).status_code == 404


def test_legacy_token_holds_only_the_default_project(tenancy):
    from examlops.data.projects import create_project

    client = TestClient(tenancy.app)
    assert _retrain(client, LEGACY).status_code == 403  # JPCP is acme's
    # An unassigned model belongs to `default`, where legacy:operator is an editor.
    create_project("default")
    conn = tenancy._get_db()
    try:
        conn.execute("DELETE FROM project_models")
        conn.execute("DELETE FROM project_resources")
        conn.commit()
    finally:
        conn.close()
    assert _retrain(client, LEGACY).status_code == 202
    # ... and a static credential with no grant cannot touch `default`'s models.
    assert (
        TestClient(tenancy.app).post("/v1/approvals/JPCP/approve", headers=EVE).status_code == 403
    )


def test_a_platform_admin_may_act_everywhere(tenancy, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTHZ_ADMINS", "ops")
    assert _retrain(TestClient(tenancy.app), OPS).status_code == 202


def test_a_model_shared_by_two_projects_needs_both(tenancy):
    from examlops.data.projects import assign_model_to_project, create_project

    create_project("beta")
    assign_model_to_project("beta", "JPCP")
    assert _retrain(TestClient(tenancy.app), BOB).status_code == 403  # editor on acme only


def test_unreadable_membership_fails_closed_with_503(tenancy, monkeypatch):
    import examlops.data as data

    def broken():
        raise RuntimeError("datastore down")

    monkeypatch.setattr(data, "get_db", broken)
    before = _metric(tenancy, "unavailable")
    r = _retrain(TestClient(tenancy.app), BOB)
    assert r.status_code == 503
    assert _metric(tenancy, "unavailable") == before + 1


# --- subject mapping ----------------------------------------------------------------------------


class _Identity:
    projects = {"acme": "operator"}


class _Ctx:
    def __init__(self, principal, *, is_legacy=False, identity=None):
        self.principal, self.is_legacy, self.identity = principal, is_legacy, identity


def test_subject_mapping():
    assert subject_of(_Ctx("legacy", is_legacy=True)) == "legacy:operator"
    assert subject_of(_Ctx("bridge")) == "bridge"
    assert asserted_projects(_Ctx("bridge")) is None
    assert asserted_projects(_Ctx("u-1", identity=_Identity())) == {"acme": "operator"}


def test_an_idp_asserted_project_role_counts(tenancy):
    from cplane.project_gate import enforce_model

    enforce_model(_Ctx("u-1", identity=_Identity()), "JPCP", "editor")  # operator ⇒ editor
    with pytest.raises(Exception) as exc:
        enforce_model(_Ctx("u-1", identity=_Identity()), "JPCP", "owner")
    assert getattr(exc.value, "status_code", None) == 403

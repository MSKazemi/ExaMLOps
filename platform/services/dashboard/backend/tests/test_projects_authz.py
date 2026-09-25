"""ADR 0014 decision 4: the dashboard project routers refuse a denied subject (HTTP 403).

Password sessions have no per-user identity; they map to ``legacy:<role>`` and hold only the
migration grants on project ``default`` (ADR 0014 decision 5). With ``EXAMLOPS_MULTITENANCY`` off
nothing changes (the existing ``test_projects_*`` suites are that proof).
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def tenant(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    dbconn.connect(db, row_factory=None).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    monkeypatch.delenv("EXAMLOPS_OPENFGA_URL", raising=False)
    from examlops.platform_db import init_db

    init_db()
    return str(db)


async def _login(client, pw):
    r = await client.post("/api/auth/login", json={"password": pw})
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _mk(client, h, name):
    return await client.post("/api/v1/projects", json={"name": name, "cpuLimit": 2}, headers=h)


def _grant(subject, relation, project):
    from examlops import authz

    authz.grant(subject, relation, f"project:{project}")


async def test_legacy_admin_holds_no_rights_on_a_non_default_project(client, tenant):
    h = await _login(client, ADMIN_PW)
    assert (await _mk(client, h, "research")).status_code == 201  # creation itself is not gated
    for method, url, kw in (
        ("put", "/api/v1/projects/research", {"json": {"cpuLimit": 8}}),
        ("post", "/api/v1/projects/research/resources", {"json": {"kind": "model", "ref": "M"}}),
        (
            "post",
            "/api/v1/projects/research/members",
            {"json": {"subject": "eve", "role": "owner"}},
        ),
        ("delete", "/api/v1/projects/research/members/eve", {}),
        ("post", "/api/v1/projects/research/storage", {"json": {}}),
        ("get", "/api/v1/projects/research", {}),
        ("delete", "/api/v1/projects/research", {}),
    ):
        r = await getattr(client, method)(url, headers=h, **kw)
        assert r.status_code == 403, (method, url, r.text)
        assert "lacks" in r.json()["detail"]
    # ... and nothing was changed or deleted.
    from examlops.data.projects import get_project

    assert get_project("research")["cpu_limit"] == 2


async def test_an_explicit_grant_unlocks_exactly_that_relation(client, tenant):
    h = await _login(client, ADMIN_PW)
    await _mk(client, h, "research")
    _grant("legacy:admin", "editor", "research")
    assert (await client.get("/api/v1/projects/research", headers=h)).status_code == 200
    assert (
        await client.put("/api/v1/projects/research", json={"cpuLimit": 8}, headers=h)
    ).status_code == 200
    # editor is not owner: people-management and deletion stay refused
    r = await client.post(
        "/api/v1/projects/research/members", json={"subject": "eve", "role": "viewer"}, headers=h
    )
    assert r.status_code == 403
    assert (await client.delete("/api/v1/projects/research", headers=h)).status_code == 403
    _grant("legacy:admin", "owner", "research")
    assert (await client.delete("/api/v1/projects/research", headers=h)).status_code == 200


async def test_legacy_viewer_reads_only_default(client, tenant):
    admin = await _login(client, ADMIN_PW)
    viewer = await _login(client, VIEWER_PW)
    await _mk(client, admin, "default")
    await _mk(client, admin, "research")
    assert (await client.get("/api/v1/projects/default", headers=viewer)).status_code == 200
    assert (await client.get("/api/v1/projects/research", headers=viewer)).status_code == 403
    listing = await client.get("/api/v1/projects", headers=viewer)
    assert [p["name"] for p in listing.json()] == ["default"]  # others are not even listed


async def test_default_project_migration_grants_for_legacy_admin(client, tenant):
    h = await _login(client, ADMIN_PW)
    await _mk(client, h, "default")
    r = await client.put("/api/v1/projects/default", json={"cpuLimit": 6}, headers=h)
    assert r.status_code == 200, r.text  # legacy admin = owner of `default`
    assert (await client.delete("/api/v1/projects/default", headers=h)).status_code == 200


async def test_role_gate_still_applies_before_relations(client, tenant):
    """A viewer session is refused by the role check even if it were granted owner on the project."""
    admin = await _login(client, ADMIN_PW)
    viewer = await _login(client, VIEWER_PW)
    await _mk(client, admin, "research")
    _grant("legacy:viewer", "owner", "research")
    r = await client.put("/api/v1/projects/research", json={"cpuLimit": 8}, headers=viewer)
    assert r.status_code == 403


async def test_denials_are_audited(client, tenant):
    h = await _login(client, ADMIN_PW)
    await _mk(client, h, "research")
    await client.get("/api/v1/projects/research", headers=h)
    conn = dbconn.connect(tenant, row_factory=None)
    n = conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='authz_deny'").fetchone()[0]
    conn.close()
    assert n >= 1


async def test_flag_off_everything_is_allowed(client, tenant, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY")
    h = await _login(client, ADMIN_PW)
    await _mk(client, h, "research")
    assert (await client.get("/api/v1/projects/research", headers=h)).status_code == 200
    assert (await client.delete("/api/v1/projects/research", headers=h)).status_code == 200


def test_federated_principal_uses_its_own_subject_and_idp_project_roles(tenant):
    from routers.projects import _authz_subject, _project_allowed

    alice = {"idp": "acme-idc", "sub": "acme-idc:alice", "role": "admin"}
    assert _authz_subject(alice) == "acme-idc:alice"
    assert not _project_allowed(alice, "viewer", "research")
    _grant("acme-idc:alice", "editor", "research")
    assert _project_allowed(alice, "editor", "research")
    assert not _project_allowed(alice, "owner", "research")
    # a project role asserted by the IdP (ADR 0120) counts too
    bob = {
        "idp": "acme-idc",
        "sub": "acme-idc:bob",
        "role": "admin",
        "projects": {"research": "owner"},
    }
    assert _project_allowed(bob, "owner", "research")
    assert not _project_allowed(bob, "viewer", "other")


async def test_creator_registration_for_federated_principals(client, tenant):
    from examlops.authz import guard

    assert guard.register_creator("acme-idc:alice", "fresh") is True
    from examlops import authz

    assert authz.check("acme-idc:alice", "owner", "project:fresh")
    assert guard.register_creator("legacy:admin", "fresh2") is False  # legacy sessions: no grant


async def test_attaching_a_model_needs_editor_where_it_lives_now(client, tenant):
    """Membership is the authorization key: editing the target project cannot claim a model."""
    from examlops.authz import guard
    from examlops.data.projects import assign_model_to_project, create_project

    h = await _login(client, ADMIN_PW)
    await _mk(client, h, "research")
    _grant("legacy:admin", "editor", "research")
    create_project("acme")
    assign_model_to_project("acme", "JPCP")
    r = await client.post(
        "/api/v1/projects/research/resources", json={"kind": "model", "ref": "JPCP"}, headers=h
    )
    assert r.status_code == 403, r.text
    assert guard.model_projects("JPCP") == ["acme"]
    # An unassigned model is `default`'s, where legacy:admin is owner: it may move it.
    r = await client.post(
        "/api/v1/projects/research/resources", json={"kind": "model", "ref": "FREE"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert guard.model_projects("FREE") == ["research"]

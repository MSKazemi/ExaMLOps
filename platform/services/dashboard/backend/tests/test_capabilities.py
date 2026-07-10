"""Capability model + tenant scoping (F15 / ADR 0057)."""

import capabilities as cap
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


# ── capability catalogue (F15 R2/R3) ─────────────────────────────────────────


def test_viewer_has_only_read_capabilities():
    caps = set(cap.capabilities_for("viewer"))
    assert caps == {cap.VIEW, cap.SEARCH}
    assert not cap.can("viewer", cap.MODEL_PROMOTE)


def test_admin_has_governed_capabilities():
    assert cap.can("admin", cap.MODEL_PROMOTE)
    assert cap.can("admin", cap.APPROVAL_DECIDE)
    assert cap.can("admin", cap.SECRET_REVEAL)


def test_unknown_role_denied_by_default():
    assert cap.capabilities_for("ghost") == []
    assert not cap.can("ghost", cap.VIEW)


def test_deny_reason_explains():
    assert cap.deny_reason("viewer", cap.MODEL_PROMOTE) == "Requires the admin role."
    assert cap.deny_reason("admin", cap.MODEL_PROMOTE) == ""  # allowed → no reason


def test_step_up_flagged_for_governed_actions():
    assert cap.requires_step_up(cap.MODEL_PROMOTE)
    assert cap.requires_step_up(cap.SECRET_REVEAL)
    assert not cap.requires_step_up(cap.SEARCH)


# ── principal ────────────────────────────────────────────────────────────────


def test_principal_defaults_tenant_and_lists_caps():
    p = cap.principal_from_claims({"role": "viewer"})
    assert p["tenant"] == "default"
    assert p["role"] == "viewer"
    assert cap.SEARCH in p["capabilities"]


# ── tenant scoping (F15 R4, default-deny) ────────────────────────────────────


def test_same_tenant_visible_cross_tenant_denied_for_viewer():
    p = cap.principal_from_claims({"role": "viewer", "tenant": "alpha"})
    assert cap.tenant_visible(p, "alpha")
    assert not cap.tenant_visible(p, "beta")


def test_admin_sees_cross_tenant():
    p = cap.principal_from_claims({"role": "admin", "tenant": "alpha"})
    assert cap.tenant_visible(p, "beta")


def test_scope_to_tenant_filters_rows():
    p = cap.principal_from_claims({"role": "viewer", "tenant": "alpha"})
    rows = [{"tenant": "alpha"}, {"tenant": "beta"}, {"id": 3}]  # 3rd is 'default'
    scoped = cap.scope_to_tenant(p, rows)
    assert scoped == [{"tenant": "alpha"}]


def test_assert_tenant_access_raises_403():
    from fastapi import HTTPException

    p = cap.principal_from_claims({"role": "viewer", "tenant": "alpha"})
    with pytest.raises(HTTPException) as ei:
        cap.assert_tenant_access(p, "beta")
    assert ei.value.status_code == 403


# ── /me capability list (F15) ────────────────────────────────────────────────


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_me_returns_capabilities_for_viewer(client):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "default"
    assert set(body["capabilities"]) == {cap.VIEW, cap.SEARCH}


@pytest.mark.asyncio
async def test_me_returns_governed_capabilities_for_admin(client):
    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert cap.MODEL_PROMOTE in r.json()["capabilities"]


# ── require_capability dependency (F15 R2) ───────────────────────────────────


def test_require_capability_dependency():
    """The guard dependency rejects a role lacking the capability (403) and passes one that has it."""
    from fastapi import HTTPException

    guard = cap.require_capability(cap.MODEL_PROMOTE)
    # the returned dependency takes the verified claims; call it directly
    with pytest.raises(HTTPException) as ei:
        guard(claims={"role": "viewer"})
    assert ei.value.status_code == 403

    assert guard(claims={"role": "admin"}) == {"role": "admin"}

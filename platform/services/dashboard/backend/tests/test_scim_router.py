"""ADR 0132 — SCIM 2.0 at /api/scim/v2, and what deprovisioning does to a live dashboard session.

Reuses the real-HTTP fake IdP and the SSO driver from test_sso_federation, so the session under
test is a genuine BFF cookie session obtained through authorization code + PKCE.
"""

from __future__ import annotations

import pytest

from tests.test_sso_federation import _cookies, _sso_login, center  # noqa: F401 — fixture reuse

SCIM_BEARER = "-".join(("jsc", "scim", "client", "credential"))
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.fixture
def scim_center(center, monkeypatch):  # noqa: F811 — the imported fixture, extended
    idp, pdp, write = center
    monkeypatch.setenv("JSC_SCIM", SCIM_BEARER)
    write(provisioning={"token_ref": "env:JSC_SCIM"})
    from examlops.iam import directory

    directory.invalidate()
    return idp, write


def _scim(token: str = SCIM_BEARER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/scim+json"}


async def _find(client, username: str) -> dict:
    resp = await client.get(
        "/api/scim/v2/Users", params={"filter": f'userName eq "{username}"'}, headers=_scim()
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_scim_speaks_scim_over_http(client, scim_center):
    created = await client.post(
        "/api/scim/v2/Users",
        headers=_scim(),
        json={
            "schemas": [USER_SCHEMA],
            "userName": "carol",
            "emails": [{"value": "c@jsc.example"}],
        },
    )
    assert created.status_code == 201
    assert created.headers["content-type"].startswith("application/scim+json")
    uid = created.json()["id"]
    assert (await _find(client, "carol"))["Resources"][0]["id"] == uid
    spc = await client.get("/api/scim/v2/ServiceProviderConfig", headers=_scim())
    assert spc.status_code == 200 and spc.json()["patch"]["supported"] is True
    gone = await client.delete(f"/api/scim/v2/Users/{uid}", headers=_scim())
    assert gone.status_code == 204
    assert (await client.get(f"/api/scim/v2/Users/{uid}", headers=_scim())).status_code == 404


async def test_scim_refuses_anyone_but_the_centers_client(client, scim_center):
    for headers in ({}, _scim("not-the-scim-credential"), {"Authorization": "Basic eDp5"}):
        resp = await client.get("/api/scim/v2/Users", headers=headers)
        assert resp.status_code == 401
        body = resp.json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert resp.headers["www-authenticate"].startswith("Bearer")


async def test_deprovisioning_ends_a_live_dashboard_session(client, scim_center):
    idp, _ = scim_center
    *_, cb = await _sso_login(client, idp, groups=["mlops-ops"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"
    assert (await client.get("/api/auth/me", headers={"Cookie": cookie})).status_code == 200
    # The SSO login recorded the account just in time; the center now withdraws it.
    uid = (await _find(client, "alice"))["Resources"][0]["id"]
    resp = await client.patch(
        f"/api/scim/v2/Users/{uid}",
        headers=_scim(),
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
        },
    )
    assert resp.status_code == 200 and resp.json()["active"] is False
    me = await client.get("/api/auth/me", headers={"Cookie": cookie})
    assert me.status_code == 401 and "deactivated" in me.json()["detail"]
    # An IdP access token for the same person is refused too, although it is still valid.
    bearer = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {idp.mint(groups=['mlops-ops'])}"}
    )
    assert bearer.status_code == 401


async def test_a_fresh_idp_login_cannot_override_a_deactivation(client, scim_center):
    idp, _ = scim_center
    await _sso_login(client, idp, groups=["mlops-ops"])  # first sign-in: account recorded
    uid = (await _find(client, "alice"))["Resources"][0]["id"]
    await client.delete(f"/api/scim/v2/Users/{uid}", headers=_scim())
    *_, cb = await _sso_login(client, idp, groups=["mlops-ops"])
    assert "sso_error=account_disabled" in cb.headers["location"]
    assert "__Host-examlops_session" not in _cookies(cb)


async def test_strict_mode_admits_only_provisioned_users(client, scim_center):
    idp, write = scim_center
    write(provisioning={"mode": "scim", "token_ref": "env:JSC_SCIM"})
    *_, refused = await _sso_login(client, idp, groups=["mlops-ops"])
    assert "sso_error=account_disabled" in refused.headers["location"]
    await client.post(
        "/api/scim/v2/Users", headers=_scim(), json={"schemas": [USER_SCHEMA], "userName": "alice"}
    )
    from examlops.iam import directory

    directory.invalidate()
    *_, admitted = await _sso_login(client, idp, groups=["mlops-ops"])
    assert "__Host-examlops_session" in _cookies(admitted)

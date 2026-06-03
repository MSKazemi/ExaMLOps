"""HTTP auth flow: login, logout, me, role gate."""

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.mark.asyncio
async def test_login_with_viewer_password_returns_viewer_role(client):
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "viewer"
    assert isinstance(body["token"], str) and len(body["token"]) > 20
    assert "expires_at" in body


@pytest.mark.asyncio
async def test_login_with_admin_password_returns_admin_role(client):
    r = await client.post("/api/auth/login", json={"password": ADMIN_PW})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_login_with_wrong_password_returns_401(client):
    r = await client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid password"


@pytest.mark.asyncio
async def test_login_with_empty_password_returns_401(client):
    r = await client.post("/api/auth/login", json={"password": ""})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid password"


@pytest.mark.asyncio
async def test_me_returns_role_for_valid_token(client):
    login = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    token = login.json()["token"]
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"


@pytest.mark.asyncio
async def test_me_without_token_returns_401(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_logout_returns_204(client):
    login = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    token = login.json()["token"]
    r = await client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 204

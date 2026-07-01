"""GET /api/dataplane/config and GET /api/dataplane/status."""

import pytest
from httpx import AsyncClient


async def _login_admin(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "test-admin-pw"})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"


async def _login_viewer(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "test-viewer-pw"})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"


@pytest.mark.asyncio
async def test_dataplane_config_requires_auth(client: AsyncClient) -> None:
    """Unauthenticated request must be rejected."""
    resp = await client.get("/api/dataplane/config")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_dataplane_config_returns_set_keys(client: AsyncClient) -> None:
    """GET /api/dataplane/config returns keys previously saved via PUT /api/config."""
    await _login_admin(client)
    await client.put(
        "/api/config",
        json={
            "dataplane_host": "sbhost",
            "dataplane_port": "<PORT>",
            "dataplane_mode": "both",
        },
    )
    resp = await client.get("/api/dataplane/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dataplane_host"] == "sbhost"
    assert data["dataplane_port"] == "<PORT>"


@pytest.mark.asyncio
async def test_dataplane_config_viewer_can_read(client: AsyncClient) -> None:
    """Viewer role can read DataPlane config."""
    await _login_viewer(client)
    resp = await client.get("/api/dataplane/config")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dataplane_status_unreachable_when_bridge_down(client: AsyncClient) -> None:
    """GET /api/dataplane/status returns reachable=False when bridge not running."""
    await _login_viewer(client)
    resp = await client.get("/api/dataplane/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "reachable" in data
    assert data["reachable"] is False
    assert "status_url" in data

"""DataPlane config keys: accepted by PUT /api/config, returned by GET /api/config."""

import pytest
from httpx import AsyncClient


async def _login_admin(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "test-admin-pw"})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"


_DATAPLANE_PAYLOAD = {
    "dataplane_host": "myhost",
    "dataplane_port": "<PORT>",
    "dataplane_mode": "both",
    "dataplane_job_topic_uuid": "<UUID>",
    "dataplane_result_topic_uuid": "<UUID>",
    "dataplane_inference_uuid": "<UUID>",
    "dataplane_retrain_uuid": "<UUID>",
    "dataplane_default_model": "JPCP",
    "dataplane_default_alias": "Production",
    "dataplane_bridge_status_url": "http://localhost:8003",
}


@pytest.mark.asyncio
async def test_dataplane_keys_accepted(client: AsyncClient) -> None:
    await _login_admin(client)
    resp = await client.put("/api/config", json=_DATAPLANE_PAYLOAD)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_dataplane_keys_returned_after_set(client: AsyncClient) -> None:
    await _login_admin(client)
    await client.put("/api/config", json=_DATAPLANE_PAYLOAD)
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dataplane_host"] == "myhost"
    assert data["dataplane_port"] == "<PORT>"
    assert data["dataplane_mode"] == "both"
    assert data["dataplane_job_topic_uuid"] == "<UUID>"
    assert data["dataplane_bridge_status_url"] == "http://localhost:8003"


@pytest.mark.asyncio
async def test_unknown_dataplane_key_rejected(client: AsyncClient) -> None:
    await _login_admin(client)
    resp = await client.put("/api/config", json={"dataplane_unknown_xyz": "value"})
    assert resp.status_code == 400

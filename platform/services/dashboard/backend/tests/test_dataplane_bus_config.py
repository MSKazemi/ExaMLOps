"""Dataplane bus config keys: accepted by PUT /api/config, returned by GET /api/config."""

import pytest
from httpx import AsyncClient


async def _login_admin(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "test-admin-pw"})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"


_DATAPLANE_BUS_PAYLOAD = {
    "dataplane_bus_host": "myhost",
    "dataplane_bus_port": "5398",
    "dataplane_bus_mode": "both",
    "dataplane_bus_job_topic_uuid": "00000000-0000-0000-0000-000000000001",
    "dataplane_bus_result_topic_uuid": "00000000-0000-0000-0000-000000000002",
    "dataplane_bus_inference_uuid": "00000000-0000-0000-0000-000000000003",
    "dataplane_bus_retrain_uuid": "00000000-0000-0000-0000-000000000004",
    "dataplane_bus_default_model": "JPCP",
    "dataplane_bus_default_alias": "Production",
    "dataplane_bus_bridge_status_url": "http://localhost:8003",
}


@pytest.mark.asyncio
async def test_dataplane_bus_keys_accepted(client: AsyncClient) -> None:
    await _login_admin(client)
    resp = await client.put("/api/config", json=_DATAPLANE_BUS_PAYLOAD)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_dataplane_bus_keys_returned_after_set(client: AsyncClient) -> None:
    await _login_admin(client)
    await client.put("/api/config", json=_DATAPLANE_BUS_PAYLOAD)
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dataplane_bus_host"] == "myhost"
    assert data["dataplane_bus_port"] == "5398"
    assert data["dataplane_bus_mode"] == "both"
    assert data["dataplane_bus_job_topic_uuid"] == "00000000-0000-0000-0000-000000000001"
    assert data["dataplane_bus_bridge_status_url"] == "http://localhost:8003"


@pytest.mark.asyncio
async def test_unknown_dataplane_bus_key_rejected(client: AsyncClient) -> None:
    await _login_admin(client)
    resp = await client.put("/api/config", json={"dataplane_bus_unknown_xyz": "value"})
    assert resp.status_code == 400

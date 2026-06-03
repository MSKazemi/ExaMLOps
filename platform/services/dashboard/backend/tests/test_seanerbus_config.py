"""SeanerBUS config keys: accepted by PUT /api/config, returned by GET /api/config."""
import pytest
from httpx import AsyncClient


async def _login_admin(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "test-admin-pw"})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"


_SEANERBUS_PAYLOAD = {
    "seanerbus_host": "myhost",
    "seanerbus_port": "5398",
    "seanerbus_mode": "both",
    "seanerbus_job_topic_uuid": "00000000-0000-0000-0000-000000000001",
    "seanerbus_result_topic_uuid": "00000000-0000-0000-0000-000000000002",
    "seanerbus_inference_uuid": "00000000-0000-0000-0000-000000000003",
    "seanerbus_retrain_uuid": "00000000-0000-0000-0000-000000000004",
    "seanerbus_default_model": "JPCP",
    "seanerbus_default_alias": "Production",
    "seanerbus_bridge_status_url": "http://localhost:8003",
}


@pytest.mark.asyncio
async def test_seanerbus_keys_accepted(client: AsyncClient) -> None:
    await _login_admin(client)
    resp = await client.put("/api/config", json=_SEANERBUS_PAYLOAD)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_seanerbus_keys_returned_after_set(client: AsyncClient) -> None:
    await _login_admin(client)
    await client.put("/api/config", json=_SEANERBUS_PAYLOAD)
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["seanerbus_host"] == "myhost"
    assert data["seanerbus_port"] == "5398"
    assert data["seanerbus_mode"] == "both"
    assert data["seanerbus_job_topic_uuid"] == "00000000-0000-0000-0000-000000000001"
    assert data["seanerbus_bridge_status_url"] == "http://localhost:8003"


@pytest.mark.asyncio
async def test_unknown_seanerbus_key_rejected(client: AsyncClient) -> None:
    await _login_admin(client)
    resp = await client.put("/api/config", json={"seanerbus_unknown_xyz": "value"})
    assert resp.status_code == 400

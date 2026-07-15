"""Tests for GET /api/seanerbus/grafana-panels endpoint."""

from tests.conftest import VIEWER_PW


async def _login_viewer(client) -> dict:
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_grafana_panels_returns_correct_shape(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_GRAFANA_URL", "http://grafana-test:3000")
    headers = await _login_viewer(client)
    response = await client.get("/api/seanerbus/grafana-panels", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["grafana_url"] == "http://grafana-test:3000"
    assert data["dashboard_uid"] == "examlops-seanerbus"
    assert data["panels"]["bridge_up"] == 1
    assert data["panels"]["inference_rate"] == 2
    assert data["panels"]["error_rate"] == 3
    assert data["panels"]["latency"] == 4


async def test_grafana_panels_default_grafana_url(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_GRAFANA_URL", raising=False)
    headers = await _login_viewer(client)
    response = await client.get("/api/seanerbus/grafana-panels", headers=headers)
    assert response.status_code == 200
    assert response.json()["grafana_url"] == "http://localhost:13000"


async def test_grafana_panels_requires_auth(client):
    response = await client.get("/api/seanerbus/grafana-panels")
    assert response.status_code == 401

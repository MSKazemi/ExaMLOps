from unittest.mock import AsyncMock, patch

import pytest

HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def clear_health_cache():
    """Reset module-level health cache between tests to prevent state bleed."""
    import routers.health as h
    h._cache.clear()
    h._probe_in_progress.clear()
    yield
    h._cache.clear()
    h._probe_in_progress.clear()


async def test_health_returns_ok_when_all_up(client):
    mock_response = AsyncMock()
    mock_response.status_code = 200

    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_ctx

        response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "services" in body
    assert "mlflow" in body["services"]
    assert "checked_at" in body


async def test_health_degraded_when_service_down(client):
    import httpx as httpx_lib

    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(side_effect=httpx_lib.ConnectError("refused"))
        mock_client_cls.return_value = mock_ctx

        response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    # Synthetic services are not HTTP-pinged: postgres (DB), dashboard (self), slurm (mock), seanerbus_sim (proxied).
    _SYNTHETIC = {"postgres", "dashboard", "slurm", "seanerbus_sim"}
    for key, svc in body["services"].items():
        if key not in _SYNTHETIC:
            assert svc["status"] == "down", f"{key} expected down, got {svc['status']}"


async def test_health_no_auth_required(client):
    """Health endpoint must be callable without a token."""
    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=AsyncMock(status_code=200))
        mock_client_cls.return_value = mock_ctx

        response = await client.get("/api/health")  # no Authorization header

    assert response.status_code == 200


async def test_health_contains_all_services(client):
    mock_response = AsyncMock()
    mock_response.status_code = 200

    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_ctx

        response = await client.get("/api/health")

    body = response.json()
    assert set(body["services"].keys()) == {
        "mlflow", "prefect", "ray_serve", "prometheus",
        "grafana", "minio", "control_plane", "postgres",
        "loki", "seanerbus", "jupyterhub",
        "dashboard", "slurm", "seanerbus_sim",
    }

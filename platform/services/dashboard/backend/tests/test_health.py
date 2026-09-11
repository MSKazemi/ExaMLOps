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
        "mlflow",
        "prefect",
        "ray_serve",
        "prometheus",
        "grafana",
        "minio",
        "control_plane",
        "postgres",
        "loki",
        "seanerbus",
        "jupyterhub",
        "dashboard",
        "slurm",
        "seanerbus_sim",
        "dataplane",
    }


# ── nothing unprobed may carry a verdict ────────────────────────────────────────────────
#
# `/api/health` used to answer `slurm: ok` whenever the platform ran in mock mode — a green tick
# for a scheduler that is not involved in the run at all — and `slurm: down` in every other mode,
# which is the case where a scheduler *does* exist and the dashboard simply cannot see it from
# here. That second branch pinned the top-level `status` at `degraded` for the entire life of any
# real-scheduler deployment, which is how a health endpoint teaches its operators to ignore it.
# `seanerbus_sim` had the same defect from the other side: it copied the bridge's HTTP
# reachability, but the bridge's own `/health` is a constant `{"status": "ok", …}` that says
# nothing about whether the Cap'n Proto bus is connected.


def _up_client():
    """A patched httpx client where every probe answers 200."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_ctx.get = AsyncMock(return_value=AsyncMock(status_code=200))
    return mock_ctx


async def test_unprobed_services_report_unknown_and_say_why(client):
    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _up_client()
        body = (await client.get("/api/health")).json()

    assert body["unmeasured"] == ["seanerbus_sim", "slurm"]
    for key in body["unmeasured"]:
        svc = body["services"][key]
        assert svc["status"] == "unknown", f"{key} claimed {svc['status']} without probing anything"
        assert svc.get("note"), f"{key} is unmeasured but does not say why"


async def test_a_real_scheduler_does_not_hold_the_platform_at_degraded(client, monkeypatch):
    """The regression: mode != mock used to mean `slurm: down` and so `status: degraded`, always."""
    import routers.health as h

    monkeypatch.setattr(h.settings, "slurm_mode", "slurm")
    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _up_client()
        body = (await client.get("/api/health")).json()

    assert body["status"] == "ok"
    assert body["services"]["slurm"]["status"] == "unknown"
    assert "slurm" in body["services"]["slurm"]["note"]


async def test_an_unmeasured_service_cannot_mask_a_real_outage(client):
    """The converse guard: excluding the unprobed entries must not make `status` unfalsifiable."""
    import httpx as httpx_lib

    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(side_effect=httpx_lib.ConnectError("refused"))
        mock_client_cls.return_value = mock_ctx
        body = (await client.get("/api/health")).json()

    assert body["status"] == "degraded"

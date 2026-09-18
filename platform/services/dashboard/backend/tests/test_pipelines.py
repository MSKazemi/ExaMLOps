"""Pipelines router tests — deployments, runs, trigger."""

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, pw: str) -> str:
    r = await client.post("/api/auth/login", json={"password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_deployments_viewer_can_access(client):
    """GET /api/pipelines/deployments is accessible to viewers."""
    token = await _login(client, VIEWER_PW)

    fake_deployments = [{"id": "dep-1", "name": "training_flow/examlops-jpcp-nightly"}]

    with patch("routers.pipelines._post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_deployments
        r = await client.get("/api/pipelines/deployments", headers=_hdr(token))

    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert data[0]["id"] == "dep-1"
    mock_post.assert_awaited_once_with("/deployments/filter", {})


@pytest.mark.asyncio
async def test_list_runs_viewer_can_access(client):
    """GET /api/pipelines/runs is accessible to viewers."""
    token = await _login(client, VIEWER_PW)

    fake_runs = [{"id": "run-1", "state": {"type": "COMPLETED"}}]

    with patch("routers.pipelines._post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_runs
        r = await client.get("/api/pipelines/runs?limit=5", headers=_hdr(token))

    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert data[0]["id"] == "run-1"
    mock_post.assert_awaited_once_with(
        "/flow_runs/filter", {"limit": 5, "sort": "EXPECTED_START_TIME_DESC"}
    )


@pytest.mark.asyncio
async def test_trigger_requires_admin_viewer_gets_403(client):
    """POST /api/pipelines/trigger returns 403 for viewer role."""
    token = await _login(client, VIEWER_PW)

    r = await client.post(
        "/api/pipelines/trigger",
        json={"model_name": "JPCP"},
        headers=_hdr(token),
    )
    assert r.status_code == 403


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _control_plane_double(calls: list, *, retrain_status: int = 202):
    async def _call(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json"), kwargs.get("headers") or {}))
        if path == "/v1/models":
            return _Resp(200, [{"model_name": "JPCP", "datasets": ["PM100Dataset", "Other"]}])
        if retrain_status >= 400:
            return _Resp(retrain_status, {"title": "Conflict", "detail": "already in progress"})
        return _Resp(
            202,
            {"command_id": "v1:retrain:abc", "state": "pending", "status_url": "/v1/commands/x"},
        )

    return _call


@pytest.mark.asyncio
async def test_trigger_queues_through_the_control_plane(client):
    """POST /api/pipelines/trigger submits /v1/retrain — never Prefect directly (plan P1.7)."""
    token = await _login(client, ADMIN_PW)
    calls: list = []

    with patch("routers.pipelines._control_plane", new=_control_plane_double(calls)):
        r = await client.post(
            "/api/pipelines/trigger",
            json={"model_name": "JPCP", "dataset_name": "PM100Dataset", "dummy": True},
            headers=_hdr(token),
        )

    assert r.status_code == 200, r.text
    assert r.json()["command_id"] == "v1:retrain:abc"
    method, path, body, headers = calls[-1]
    assert (method, path) == ("POST", "/v1/retrain")
    assert body == {"model_name": "JPCP", "dataset_name": "PM100Dataset", "is_dummy": True}
    assert headers.get("Idempotency-Key")


@pytest.mark.asyncio
async def test_trigger_defaults_to_the_models_first_dataset(client):
    token = await _login(client, ADMIN_PW)
    calls: list = []

    with patch("routers.pipelines._control_plane", new=_control_plane_double(calls)):
        r = await client.post(
            "/api/pipelines/trigger", json={"model_name": "JPCP"}, headers=_hdr(token)
        )

    assert r.status_code == 200
    assert calls[-1][2]["dataset_name"] == "PM100Dataset"


@pytest.mark.asyncio
async def test_a_retrain_already_in_progress_is_surfaced_not_duplicated(client):
    token = await _login(client, ADMIN_PW)
    calls: list = []

    with patch(
        "routers.pipelines._control_plane", new=_control_plane_double(calls, retrain_status=409)
    ):
        r = await client.post(
            "/api/pipelines/trigger",
            json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
            headers=_hdr(token),
        )

    assert r.status_code == 409
    assert "already in progress" in r.json()["detail"]

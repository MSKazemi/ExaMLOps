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


@pytest.mark.asyncio
async def test_trigger_works_for_admin(client):
    """POST /api/pipelines/trigger creates a flow run for admin."""
    token = await _login(client, ADMIN_PW)

    fake_dep = {"id": "dep-abc", "name": "training_flow/examlops-jpcp-nightly"}
    fake_run = {"id": "run-xyz", "state": {"type": "SCHEDULED"}}

    with (
        patch("routers.pipelines._get", new_callable=AsyncMock) as mock_get,
        patch("routers.pipelines._post", new_callable=AsyncMock) as mock_post,
    ):
        mock_get.return_value = fake_dep
        mock_post.return_value = fake_run

        r = await client.post(
            "/api/pipelines/trigger",
            json={"model_name": "JPCP", "dataset_name": "PM100Dataset", "dummy": True},
            headers=_hdr(token),
        )

    assert r.status_code == 200
    data = r.json()
    assert data["flow_run_id"] == "run-xyz"
    assert data["state"] == "SCHEDULED"

    mock_get.assert_awaited_once_with("/deployments/name/training_flow/examlops-jpcp-nightly")
    mock_post.assert_awaited_once_with(
        "/deployments/dep-abc/create_flow_run",
        {
            "parameters": {
                "model_name": "JPCP",
                "dataset_name": "PM100Dataset",
                "is_dummy": True,
            }
        },
    )

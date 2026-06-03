"""Tests for alias PUT/DELETE and extended versions endpoint."""
import httpx
import pytest
from control_plane_client import ControlPlaneClient

from tests.conftest import ADMIN_PW, VIEWER_PW
from tests.fakes import make_control_plane_transport, make_mlflow_transport

_JPCP_META = {
    "name": "JPCP", "task_type": "regression",
    "estimator_class": "sklearn.ensemble.RandomForestRegressor",
    "supported_datasets": ["PM100Dataset"],
    "input_schema": {}, "output_schema": {},
    "promotion": {"metric": "rmse", "threshold": 50.0, "direction": "lower_is_better", "model_id": "jpcp"},
    "path_in_repo": "modelzoo/.../jpcp/",
    "bundled_images": [],
}

_VERSIONS = [
    {"version": "3", "run_id": "r3", "aliases": ["Staging"],
     "tags": [{"key": "framework", "value": "sklearn"}],
     "creation_timestamp": 1700000000000, "last_updated_timestamp": 1700000001000},
    {"version": "2", "run_id": "r2", "aliases": ["Production"],
     "tags": [{"key": "framework", "value": "sklearn"}],
     "creation_timestamp": 1690000000000, "last_updated_timestamp": 1690000001000},
    {"version": "1", "run_id": "r1", "aliases": ["Archived"],
     "tags": [],
     "creation_timestamp": 1680000000000, "last_updated_timestamp": 1680000001000},
]


async def _login(client, password: str) -> str:
    r = await client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fake_deps(monkeypatch):
    cp_transport = make_control_plane_transport({"JPCP": _JPCP_META})
    mlflow_transport = make_mlflow_transport({"jpcp": _VERSIONS})
    monkeypatch.setattr(
        "routers.models._control_plane",
        lambda: ControlPlaneClient(base_url="http://cp", transport=cp_transport),
    )
    monkeypatch.setattr(
        "routers.models._mlflow_client",
        lambda: httpx.AsyncClient(
            base_url="http://mlflow", transport=mlflow_transport
        ),
    )


@pytest.mark.asyncio
async def test_versions_includes_all_aliases_and_framework(client, fake_deps):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/models/JPCP/versions", headers=_hdr(token))
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) == 3
    # Most recent version first
    v3 = next(v for v in versions if v["version"] == "3")
    assert "Staging" in v3["aliases"]
    assert v3["framework"] == "sklearn"
    assert "rmse" in v3["metrics"]


@pytest.mark.asyncio
async def test_set_alias_requires_admin(client, fake_deps):
    token = await _login(client, VIEWER_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "Production"},
        headers=_hdr(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_set_production_alias_demotes_previous(client, fake_deps):
    token = await _login(client, ADMIN_PW)
    # Promote v3 to Production (currently v2 is Production)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "Production"},
        headers=_hdr(token),
    )
    assert r.status_code == 200
    versions = r.json()
    v3 = next(v for v in versions if v["version"] == "3")
    v2 = next(v for v in versions if v["version"] == "2")
    assert "Production" in v3["aliases"]
    assert "Production" not in v2["aliases"]
    assert "Archived" in v2["aliases"]


@pytest.mark.asyncio
async def test_set_invalid_alias_rejected(client, fake_deps):
    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "NotAnAlias"},
        headers=_hdr(token),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_alias_requires_admin(client, fake_deps):
    token = await _login(client, VIEWER_PW)
    r = await client.delete(
        "/api/models/JPCP/versions/3/alias/Staging",
        headers=_hdr(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_alias_removes_it(client, fake_deps):
    token = await _login(client, ADMIN_PW)
    r = await client.delete(
        "/api/models/JPCP/versions/3/alias/Staging",
        headers=_hdr(token),
    )
    assert r.status_code == 200
    versions = r.json()
    v3 = next(v for v in versions if v["version"] == "3")
    assert "Staging" not in v3["aliases"]


@pytest.mark.asyncio
async def test_delete_alias_wrong_version_returns_409(client, fake_deps):
    """DELETE on a version that doesn't hold the alias must return 409."""
    token = await _login(client, ADMIN_PW)
    # Staging belongs to version 3, not version 1 — expect 409.
    r = await client.delete(
        "/api/models/JPCP/versions/1/alias/Staging",
        headers=_hdr(token),
    )
    assert r.status_code == 409
    detail = r.json().get("detail", "")
    assert "Staging" in detail
    assert "3" in detail  # holder version mentioned in the error

"""Tests for GET /api/seanerbus/model-uuids endpoint."""
import pytest

from tests.conftest import VIEWER_PW


async def _login_viewer(client) -> dict:
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def model_yamls(tmp_path):
    (tmp_path / "jpcp.yaml").write_text(
        "name: JPCP\nmodel_class: JPCP\nconfig_class: jpcp_config.JPCPConfiguration\n"
        "task_type: regression\nframework: sklearn\nenabled: true\n"
        "seanerbus_uuid: aaaaaaaa-0000-0000-0000-000000000001\n"
    )
    (tmp_path / "mack.yaml").write_text(
        "name: MACK\nmodel_class: MACK\nconfig_class: mack_config.MACKConfiguration\n"
        "task_type: regression\nframework: sklearn\nenabled: true\n"
    )
    return tmp_path


async def test_get_model_uuids_returns_all_models(client, model_yamls, monkeypatch):
    monkeypatch.setenv("MODELS_YAML_DIR", str(model_yamls))
    headers = await _login_viewer(client)
    response = await client.get("/api/seanerbus/model-uuids", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["JPCP"] == "aaaaaaaa-0000-0000-0000-000000000001"
    assert data["MACK"] is None


async def test_get_model_uuids_requires_auth(client):
    response = await client.get("/api/seanerbus/model-uuids")
    assert response.status_code == 401


async def test_get_model_uuids_missing_dir_returns_empty(client, monkeypatch):
    monkeypatch.setenv("MODELS_YAML_DIR", "/nonexistent/dir")
    headers = await _login_viewer(client)
    response = await client.get("/api/seanerbus/model-uuids", headers=headers)
    assert response.status_code == 200
    assert response.json() == {}

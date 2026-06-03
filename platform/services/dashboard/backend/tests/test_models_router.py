import hashlib

import httpx as _httpx
import pytest
from control_plane_client import ControlPlaneClient
from settings import settings as _settings

from tests.conftest import ADMIN_PW, VIEWER_PW
from tests.fakes import make_control_plane_transport, make_mlflow_transport

_JPCP_META = {
    "name": "JPCP", "task_type": "regression",
    "estimator_class": "sklearn.ensemble.RandomForestRegressor",
    "supported_datasets": ["PM100Dataset", "FDataDataset"],
    "input_schema": {"submit_time": "float"}, "output_schema": {"power": "float"},
    "promotion": {
        "metric": "rmse", "threshold": 0.15,
        "direction": "lower_is_better", "model_id": "JPCP",
    },
    "path_in_repo": "modelzoo/.../jpcp/",
    "bundled_images": ["diagram.png"],
}


async def _login(client, password: str) -> str:
    r = await client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fake_cp(monkeypatch):
    transport = make_control_plane_transport({"JPCP": _JPCP_META})
    monkeypatch.setattr(
        "routers.models._control_plane",
        lambda: ControlPlaneClient(base_url="http://cp", transport=transport),
    )


@pytest.mark.asyncio
async def test_registry_lists_models(client, fake_cp):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/models/registry", headers=_hdr(token))
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    names = [m["name"] for m in body]
    assert "JPCP" in names
    jpcp = next(m for m in body if m["name"] == "JPCP")
    assert jpcp["task_type"] == "regression"
    assert "PM100Dataset" in jpcp["supported_datasets"]


@pytest.mark.asyncio
async def test_registry_requires_auth(client):
    response = await client.get("/api/models/registry")
    assert response.status_code == 401


@pytest.fixture
def fake_mlflow(monkeypatch):
    versions = [
        {"version": "9", "run_id": "r9", "current_stage": "None",
         "aliases": ["Staging"], "creation_timestamp": 1700000000000,
         "last_updated_timestamp": 1700000000000},
        {"version": "8", "run_id": "r8", "current_stage": "None",
         "aliases": ["Canary"], "creation_timestamp": 1690000000000,
         "last_updated_timestamp": 1690000000000},
        {"version": "7", "run_id": "r7", "current_stage": "None",
         "aliases": ["Production"], "creation_timestamp": 1680000000000,
         "last_updated_timestamp": 1680000000000},
        {"version": "6", "run_id": "r6", "current_stage": "None",
         "aliases": ["Archived"], "creation_timestamp": 1670000000000,
         "last_updated_timestamp": 1670000000000},
    ]
    transport = make_mlflow_transport({"JPCP": versions})
    monkeypatch.setattr(
        "routers.models._mlflow_client",
        lambda: _httpx.AsyncClient(base_url="http://mlflow", transport=transport),
    )


@pytest.mark.asyncio
async def test_versions_returns_per_stage(client, fake_cp, fake_mlflow):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/models/JPCP/versions", headers=_hdr(token))
    assert response.status_code == 200
    body = response.json()
    assert {row["alias"] for row in body} == {"Staging", "Canary", "Production", "Archived"}
    prod = next(row for row in body if row["alias"] == "Production")
    assert prod["version"] == "7"


@pytest.mark.asyncio
async def test_versions_unknown_model_404(client, fake_cp, fake_mlflow):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/models/Nope/versions", headers=_hdr(token))
    assert response.status_code == 404


_VALID_README = """---
display_name: "JPCP — Joint Power"
summary: "predicts power"
status: stable
paper:
  url: "https://arxiv.org/abs/x"
---
# Body
"""
_VALID_README_SHA = hashlib.sha256(_VALID_README.encode()).hexdigest()


@pytest.fixture
def fake_cp_with_readme(monkeypatch):
    transport = make_control_plane_transport(
        {"JPCP": _JPCP_META},
        readmes={"JPCP": (_VALID_README, _VALID_README_SHA)},
    )
    monkeypatch.setattr(
        "routers.models._control_plane",
        lambda: ControlPlaneClient(base_url="http://cp", transport=transport),
    )


@pytest.mark.asyncio
async def test_get_model_filesystem_default(client, fake_cp_with_readme, fake_mlflow):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/models/JPCP", headers=_hdr(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "JPCP"
    assert body["frontmatter"]["display_name"] == "JPCP — Joint Power"
    assert body["description"]["source"] == "filesystem"
    assert body["description"]["upstream_drift"] is False
    assert body["description"]["body"].startswith("# Body")
    assert body["technical"]["estimator_class"].startswith("sklearn")
    assert body["stages"]["production"] is not None
    assert body["stages"]["production"]["alias"] == "Production"
    assert "mlflow_model" in body["links"]
    # Bundled image surfaced even without uploads.
    assert any(
        img.get("source") == "filesystem" and img["placeholder"] == "images/diagram.png"
        for img in body["images"]
    )


@pytest.mark.asyncio
async def test_get_model_uses_db_override(client, fake_cp_with_readme, fake_mlflow, db_engine):
    """An override row in the DB must win over the filesystem README."""
    from models import ModelDocOverride
    from sqlalchemy.ext.asyncio import async_sessionmaker

    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as s:
        s.add(ModelDocOverride(
            model_name="JPCP",
            body="# Custom body",
            fs_sha="a" * 64,  # mismatches the README sha → drift
            updated_by="hash",
        ))
        await s.commit()

    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/models/JPCP", headers=_hdr(token))
    body = response.json()
    assert body["description"]["body"] == "# Custom body"
    assert body["description"]["source"] == "override"
    assert body["description"]["upstream_drift"] is True


@pytest.mark.asyncio
async def test_get_unknown_model_404(client, fake_cp_with_readme, fake_mlflow):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/models/DoesNotExist", headers=_hdr(token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_predict_proxies_to_ray(client, fake_cp, monkeypatch):
    captured: dict = {}

    def handler(request: _httpx.Request) -> _httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return _httpx.Response(200, json={"prediction": 0.42})

    monkeypatch.setattr(
        "routers.models._ray_client",
        lambda: _httpx.AsyncClient(base_url="http://ray", transport=_httpx.MockTransport(handler)),
    )
    token = await _login(client, VIEWER_PW)
    response = await client.post(
        "/api/models/JPCP/predict?stage=Canary",
        json={"submit_time": 1.0},
        headers=_hdr(token),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"prediction": 0.42}
    assert "stage=Canary" in captured["url"]


@pytest.mark.asyncio
async def test_put_description_viewer_forbidden(client, fake_cp_with_readme):
    token = await _login(client, VIEWER_PW)
    response = await client.put(
        "/api/models/JPCP/description",
        json={"markdown": "# Brand new"},
        headers=_hdr(token),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_put_description_admin_saves(client, fake_cp_with_readme, fake_mlflow):
    admin_token = await _login(client, ADMIN_PW)
    response = await client.put(
        "/api/models/JPCP/description",
        json={"markdown": "# Brand new"},
        headers=_hdr(admin_token),
    )
    assert response.status_code == 200, response.text

    viewer_token = await _login(client, VIEWER_PW)
    detail = await client.get("/api/models/JPCP", headers=_hdr(viewer_token))
    assert detail.json()["description"]["body"] == "# Brand new"
    assert detail.json()["description"]["source"] == "override"


@pytest.mark.asyncio
async def test_delete_description_reverts(client, fake_cp_with_readme, fake_mlflow):
    admin_token = await _login(client, ADMIN_PW)
    await client.put(
        "/api/models/JPCP/description",
        json={"markdown": "# x"},
        headers=_hdr(admin_token),
    )
    response = await client.delete(
        "/api/models/JPCP/description", headers=_hdr(admin_token),
    )
    assert response.status_code == 204

    viewer_token = await _login(client, VIEWER_PW)
    detail = await client.get("/api/models/JPCP", headers=_hdr(viewer_token))
    assert detail.json()["description"]["source"] == "filesystem"


@pytest.mark.asyncio
async def test_put_unknown_model_404(client, fake_cp_with_readme):
    admin_token = await _login(client, ADMIN_PW)
    response = await client.put(
        "/api/models/Nope/description",
        json={"markdown": "x"},
        headers=_hdr(admin_token),
    )
    assert response.status_code == 404


# ── Image upload / delete ──────────────────────────────────────────────────


class _FakeImageStorage:
    """In-memory storage stub — avoids moto/aiobotocore async compatibility issues.
    The real aioboto3 + moto integration is exercised in tests/test_storage.py."""
    def __init__(self):
        self._objects: dict[str, bytes] = {}

    async def ensure_bucket(self) -> None:
        pass

    async def put(self, *, key: str, data: bytes, content_type: str) -> None:
        self._objects[key] = data

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def presigned_get_url(self, key: str, *, expires: int) -> str:
        return f"http://fake-storage/{key}?expires={expires}"


@pytest.fixture
def fake_storage(monkeypatch):
    stub = _FakeImageStorage()
    monkeypatch.setattr("routers.models._image_storage", lambda: stub)
    return stub


@pytest.mark.asyncio
async def test_upload_image_viewer_forbidden(client, fake_cp):
    token = await _login(client, VIEWER_PW)
    response = await client.post(
        "/api/models/JPCP/images",
        headers=_hdr(token),
        files={"file": ("test.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_upload_image_admin_returns_id_and_placeholder(client, fake_cp, fake_storage):
    token = await _login(client, ADMIN_PW)
    data = b"\x89PNG\r\n\x1a\nfake"
    response = await client.post(
        "/api/models/JPCP/images",
        headers=_hdr(token),
        files={"file": ("diagram.png", data, "image/png")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "id" in body
    assert body["placeholder"].startswith("dashboard://image/")
    assert body["size_bytes"] == len(data)


@pytest.mark.asyncio
async def test_upload_image_too_large_returns_413(client, fake_cp, fake_storage, monkeypatch):
    monkeypatch.setattr(_settings, "dashboard_max_image_bytes", 10)
    token = await _login(client, ADMIN_PW)
    response = await client.post(
        "/api/models/JPCP/images",
        headers=_hdr(token),
        files={"file": ("big.png", b"x" * 11, "image/png")},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upload_image_bad_mime_returns_415(client, fake_cp):
    token = await _login(client, ADMIN_PW)
    response = await client.post(
        "/api/models/JPCP/images",
        headers=_hdr(token),
        files={"file": ("doc.pdf", b"content", "application/pdf")},
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_delete_image(client, fake_cp, fake_storage):
    token = await _login(client, ADMIN_PW)
    data = b"\x89PNG\r\n\x1a\nfake"
    upload_resp = await client.post(
        "/api/models/JPCP/images",
        headers=_hdr(token),
        files={"file": ("test.png", data, "image/png")},
    )
    assert upload_resp.status_code == 200
    image_id = upload_resp.json()["id"]

    delete_resp = await client.delete(
        f"/api/models/JPCP/images/{image_id}",
        headers=_hdr(token),
    )
    assert delete_resp.status_code == 204

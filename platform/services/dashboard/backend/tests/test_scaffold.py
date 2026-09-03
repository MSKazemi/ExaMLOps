"""Scaffold router tests — preview and create endpoints."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture(autouse=True)
def _platform_db(tmp_path, monkeypatch):
    """Keep the D11 audit write on /create inside a per-test scratch DB."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))


async def _login(client, pw: str) -> str:
    r = await client.post("/api/auth/login", json={"password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_BODY = {
    "name": "TestModel",
    "task": "performance_prediction",
    "task_type": "regression",
    "promotion_metric": "rmse",
    "promotion_threshold": 100.0,
    "promotion_direction": "lower_is_better",
}


def _mock_script(exists: bool) -> MagicMock:
    m = MagicMock()
    m.exists.return_value = exists
    m.__str__ = lambda self: "/app/tools/scaffold_model.py"  # type: ignore[method-assign,assignment,misc]
    return m


@pytest.mark.asyncio
async def test_preview_requires_admin_viewer_gets_403(client):
    """POST /api/scaffold/preview returns 403 for viewer role."""
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/scaffold/preview", json=_BODY, headers=_hdr(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_preview_returns_file_dict_when_script_exists(client):
    """POST /api/scaffold/preview returns {path: content} dict on success."""
    token = await _login(client, ADMIN_PW)

    fake_result = SimpleNamespace(
        returncode=0,
        stdout='{"modelzoo/seanergys_modelzoo/models/tasks/testmodel.py": "# model code", "pipelines/models/testmodel.yaml": "name: TestModel\\n"}',
        stderr="",
    )

    with (
        patch("routers.scaffold._SCAFFOLD_SCRIPT", _mock_script(exists=True)),
        patch("routers.scaffold.subprocess.run", return_value=fake_result),
    ):
        r = await client.post("/api/scaffold/preview", json=_BODY, headers=_hdr(token))

    assert r.status_code == 200
    data = r.json()
    assert "modelzoo/seanergys_modelzoo/models/tasks/testmodel.py" in data
    assert data["modelzoo/seanergys_modelzoo/models/tasks/testmodel.py"] == "# model code"


@pytest.mark.asyncio
async def test_preview_returns_503_when_script_missing(client):
    """POST /api/scaffold/preview returns 503 when scaffold script is absent."""
    token = await _login(client, ADMIN_PW)

    with patch("routers.scaffold._SCAFFOLD_SCRIPT", _mock_script(exists=False)):
        r = await client.post("/api/scaffold/preview", json=_BODY, headers=_hdr(token))

    assert r.status_code == 503


@pytest.mark.asyncio
async def test_create_returns_503_when_repo_root_not_set(client, monkeypatch):
    """POST /api/scaffold/create returns 503 when settings.repo_root is None."""
    from settings import settings

    token = await _login(client, ADMIN_PW)

    monkeypatch.setattr(settings, "repo_root", None)

    with patch("routers.scaffold._SCAFFOLD_SCRIPT", _mock_script(exists=True)):
        r = await client.post("/api/scaffold/create", json=_BODY, headers=_hdr(token))

    assert r.status_code == 503


@pytest.mark.asyncio
async def test_create_calls_subprocess_with_repo_root(client, monkeypatch):
    """POST /api/scaffold/create passes --repo-root and --name to subprocess."""
    from settings import settings

    token = await _login(client, ADMIN_PW)

    monkeypatch.setattr(settings, "repo_root", "/repo")

    fake_result = SimpleNamespace(
        returncode=0,
        stdout="Created 4 files for TestModel",
        stderr="",
    )

    with (
        patch("routers.scaffold._SCAFFOLD_SCRIPT", _mock_script(exists=True)),
        patch("routers.scaffold.subprocess.run", return_value=fake_result) as mock_run,
    ):
        r = await client.post("/api/scaffold/create", json=_BODY, headers=_hdr(token))

    assert r.status_code == 200
    data = r.json()
    assert "message" in data

    call_args = mock_run.call_args[0][0]
    assert "--repo-root" in call_args
    assert "/repo" in call_args
    assert "--name" in call_args
    assert "TestModel" in call_args


@pytest.mark.asyncio
async def test_create_writes_audit_event(client, monkeypatch):
    """POST /api/scaffold/create records a model_scaffolded audit event (D11)."""
    from unittest.mock import MagicMock as _MM

    from settings import settings

    token = await _login(client, ADMIN_PW)
    monkeypatch.setattr(settings, "repo_root", "/repo")

    fake_result = SimpleNamespace(returncode=0, stdout="Created 4 files", stderr="")
    mock_audit = _MM()

    with (
        patch("routers.scaffold._SCAFFOLD_SCRIPT", _mock_script(exists=True)),
        patch("routers.scaffold.subprocess.run", return_value=fake_result),
        patch("routers.scaffold.audit_write", mock_audit),
    ):
        r = await client.post("/api/scaffold/create", json=_BODY, headers=_hdr(token))

    assert r.status_code == 200
    args = mock_audit.audit.call_args[0]
    assert args[1] == "model_scaffolded"
    assert args[2] == "TestModel"
    # The actor comes from the JWT sub claim (D6), never "?".
    assert args[0] != "?"

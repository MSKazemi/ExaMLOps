from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_mock_container(service: str, status: str = "running", health: str = "healthy"):
    c = MagicMock()
    c.labels = {
        "com.docker.compose.service": service,
        "com.docker.compose.project": "test-project",
    }
    c.status = status
    c.attrs = {
        "State": {
            "StartedAt": "2026-05-18T08:00:00Z",
            "Health": {"Status": health},
        },
        # Real Docker container-list payload carries the image name here; the code must read
        # it from attrs (not ``c.image``, which triggers a forbidden /images inspect on a
        # hardened docker-socket-proxy → 403 → 500). See routers.containers._image_ref.
        "Config": {"Image": f"examlops/{service}:latest"},
        "Image": f"examlops/{service}:latest",
    }
    # A distinct value on c.image proves the code reads attrs, not c.image.
    c.image.tags = ["should-not-be-read:tag"]
    return c


def _make_client(containers):
    """Return a configured TestClient with docker mocked."""
    from routers.containers import _admin_dep, _viewer_dep, router

    app = FastAPI()
    app.dependency_overrides[_viewer_dep] = lambda: {"sub": "t", "role": "viewer"}
    app.dependency_overrides[_admin_dep] = lambda: {"sub": "t", "role": "admin"}
    app.include_router(router, prefix="/api")
    return TestClient(app)


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_list_containers_returns_all(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    mock_docker.containers.list.return_value = [
        _make_mock_container("ray-serving"),
        _make_mock_container("mlflow", status="exited"),
    ]
    mock_client_fn.return_value = mock_docker

    client = _make_client(mock_docker.containers.list.return_value)
    resp = client.get("/api/containers")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["containers"]) == 2
    assert data["containers"][0]["name"] == "ray-serving"
    assert data["containers"][0]["display_name"] == "Ray Serve"
    assert data["containers"][0]["status"] == "running"
    assert data["containers"][0]["health"] == "healthy"
    # Image comes from attrs, never c.image (which would 403 through the socket proxy).
    assert data["containers"][0]["image"] == "examlops/ray-serving:latest"
    assert data["containers"][1]["status"] == "exited"


@patch("routers.containers.get_own_project", return_value=None)
@patch("routers.containers.get_docker_client", return_value=None)
def test_list_containers_503_when_docker_unavailable(_mock_client, _mock_proj):
    from routers.containers import _viewer_dep, router

    app = FastAPI()
    app.dependency_overrides[_viewer_dep] = lambda: {"sub": "t", "role": "viewer"}
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    resp = client.get("/api/containers")
    assert resp.status_code == 503


def test_uptime_format():
    from routers.containers import _uptime

    c = MagicMock()
    c.status = "running"
    c.attrs = {"State": {"StartedAt": "2026-05-18T08:00:00Z"}}
    result = _uptime(c)
    assert "h" in result or "m" in result


def test_uptime_empty_when_not_running():
    from routers.containers import _uptime

    c = MagicMock()
    c.status = "exited"
    assert _uptime(c) == ""


def test_health_returns_status():
    from routers.containers import _health

    c = MagicMock()
    c.attrs = {"State": {"Health": {"Status": "healthy"}}}
    assert _health(c) == "healthy"


def test_health_returns_none_when_absent():
    from routers.containers import _health

    c = MagicMock()
    c.attrs = {"State": {}}
    assert _health(c) == "none"


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_start_container(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("ray-serving", status="exited")
    mock_docker.containers.list.return_value = [c]
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.post("/api/containers/ray-serving/start")
    assert resp.status_code == 200
    c.start.assert_called_once()


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_start_already_running_returns_409(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("ray-serving", status="running")
    mock_docker.containers.list.return_value = [c]
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.post("/api/containers/ray-serving/start")
    assert resp.status_code == 409


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_stop_container(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("mlflow", status="running")
    mock_docker.containers.list.return_value = [c]
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.post("/api/containers/mlflow/stop")
    assert resp.status_code == 200
    c.stop.assert_called_once()


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_stop_already_stopped_returns_409(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("mlflow", status="exited")
    mock_docker.containers.list.return_value = [c]
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.post("/api/containers/mlflow/stop")
    assert resp.status_code == 409


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_restart_container(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("ray-serving", status="running")
    own_c = _make_mock_container("dashboard", status="running")
    mock_docker.containers.list.return_value = [c]
    mock_docker.containers.get.return_value = own_c
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.post("/api/containers/ray-serving/restart")
    assert resp.status_code == 200
    c.restart.assert_called_once()


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_restart_self_returns_200_with_reconnect_payload(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("dashboard", status="running")
    mock_docker.containers.list.return_value = [c]
    mock_docker.containers.get.return_value = c  # own container is also dashboard
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.post("/api/containers/dashboard/restart")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "restarting"
    assert "reconnect_after_ms" in data


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_start_unknown_container_returns_404(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    mock_docker.containers.list.return_value = []
    mock_client_fn.return_value = mock_docker

    client = _make_client([])
    resp = client.post("/api/containers/nonexistent/start")
    assert resp.status_code == 404


@patch("routers.containers.get_own_project", return_value="test-project")
@patch("routers.containers.get_docker_client")
def test_get_logs_returns_text(mock_client_fn, _mock_proj):
    mock_docker = MagicMock()
    c = _make_mock_container("ray-serving")
    c.logs.return_value = b"2026-05-18 INFO model loaded\n2026-05-18 INFO ready\n"
    mock_docker.containers.list.return_value = [c]
    mock_client_fn.return_value = mock_docker

    client = _make_client([c])
    resp = client.get("/api/containers/ray-serving/logs?lines=50")

    assert resp.status_code == 200
    data = resp.json()
    assert "model loaded" in data["logs"]
    c.logs.assert_called_once_with(tail=50, timestamps=True)

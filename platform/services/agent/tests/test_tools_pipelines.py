import httpx
import respx
from exa_agent import confirm
from exa_agent.tools import _http, pipelines


def _patch_dashboard(monkeypatch):
    monkeypatch.setattr(
        _http, "_DASHBOARD", _http.DashboardClient(base_url="http://localhost:18099", password="pw")
    )


def _login_mock():
    respx.post("http://localhost:18099/api/login").mock(
        return_value=httpx.Response(200, json={"token": "T", "role": "admin", "expires_at": "x"})
    )


@respx.mock
def test_list_deployments(monkeypatch):
    _patch_dashboard(monkeypatch)
    _login_mock()
    respx.get("http://localhost:18099/api/pipelines/deployments").mock(
        return_value=httpx.Response(200, json=[{"name": "training_flow/examlops-jpcp-nightly"}])
    )
    out = pipelines.list_deployments.invoke({})
    assert "jpcp" in out


@respx.mock
def test_scaffold_preview_is_readonly(monkeypatch):
    _patch_dashboard(monkeypatch)
    _login_mock()
    respx.post("http://localhost:18099/api/scaffold/preview").mock(
        return_value=httpx.Response(200, json={"pipelines/models/demoad.yaml": "name: DemoAD"})
    )
    out = pipelines.scaffold_preview.invoke(
        {"name": "DemoAD", "task": "anomaly_detection", "task_type": "classification"}
    )
    assert "demoad.yaml" in out


@respx.mock
def test_scaffold_create_confirmed(monkeypatch):
    _patch_dashboard(monkeypatch)
    _login_mock()
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    respx.post("http://localhost:18099/api/scaffold/create").mock(
        return_value=httpx.Response(200, json={"message": "created 4 files"})
    )
    out = pipelines.scaffold_create.invoke(
        {"name": "DemoAD", "task": "anomaly_detection", "task_type": "classification"}
    )
    assert "created" in out


def test_scaffold_create_cancelled(monkeypatch):
    _patch_dashboard(monkeypatch)
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    out = pipelines.scaffold_create.invoke(
        {"name": "DemoAD", "task": "anomaly_detection", "task_type": "classification"}
    )
    assert out == "Cancelled — no action taken."

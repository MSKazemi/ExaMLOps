import httpx
import respx
from exa_agent import confirm
from exa_agent.tools import _http, services


def _patch_dashboard(monkeypatch):
    monkeypatch.setattr(
        _http, "_DASHBOARD", _http.DashboardClient(base_url="http://localhost:18099", password="pw")
    )


@respx.mock
def test_list_services(monkeypatch):
    _patch_dashboard(monkeypatch)
    respx.post("http://localhost:18099/api/login").mock(
        return_value=httpx.Response(200, json={"token": "T", "role": "admin", "expires_at": "x"})
    )
    respx.get("http://localhost:18099/api/containers").mock(
        return_value=httpx.Response(200, json=[{"name": "mlflow", "status": "running"}])
    )
    out = services.list_services.invoke({})
    assert "mlflow" in out


@respx.mock
def test_restart_confirmed(monkeypatch):
    _patch_dashboard(monkeypatch)
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    respx.post("http://localhost:18099/api/login").mock(
        return_value=httpx.Response(200, json={"token": "T", "role": "admin", "expires_at": "x"})
    )
    respx.post("http://localhost:18099/api/containers/mlflow/restart").mock(
        return_value=httpx.Response(200, json={"restarted": "mlflow"})
    )
    out = services.restart_service.invoke({"service": "mlflow"})
    assert "mlflow" in out


def test_stop_cancelled(monkeypatch):
    _patch_dashboard(monkeypatch)
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    out = services.stop_service.invoke({"service": "mlflow"})
    assert out == "Cancelled — no action taken."

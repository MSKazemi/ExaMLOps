import httpx
import respx
from exa_agent import confirm
from exa_agent.tools import modelzoo


@respx.mock
def test_status():
    respx.get("http://localhost:18002/modelzoo/status").mock(
        return_value=httpx.Response(200, json={"jpcp": "fresh"})
    )
    out = modelzoo.modelzoo_status.invoke({})
    assert "jpcp" in out


@respx.mock
def test_sync_confirmed(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    respx.post("http://localhost:18002/modelzoo/sync").mock(
        return_value=httpx.Response(200, json={"checked": 3})
    )
    out = modelzoo.modelzoo_sync.invoke({})
    assert "3" in out or "checked" in out


@respx.mock
def test_set_config_confirmed(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    respx.put("http://localhost:18002/modelzoo/config").mock(
        return_value=httpx.Response(200, json={"auto_retrain": True})
    )
    out = modelzoo.modelzoo_set_config.invoke({"auto_retrain": True})
    assert "auto_retrain" in out

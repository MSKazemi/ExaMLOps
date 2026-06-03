import httpx
import respx
from exa_agent import confirm
from exa_agent.tools import inference


@respx.mock
def test_predict_success():
    respx.post("http://localhost:18001/predict/JPCP").mock(
        return_value=httpx.Response(200, json={"prediction": 142.7, "model_version": "3", "alias": "Production"})
    )
    out = inference.predict.invoke({"model_name": "JPCP", "features": [1.0, 2.0]})
    assert "142.7" in out and "v3" in out


@respx.mock
def test_list_loaded_models():
    respx.get("http://localhost:18001/models").mock(
        return_value=httpx.Response(200, json={"models": ["jpcp", "mack"]})
    )
    out = inference.list_loaded_models.invoke({})
    assert "jpcp" in out


@respx.mock
def test_reload_models_confirmed(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    respx.post("http://localhost:18001/reload").mock(return_value=httpx.Response(200, json={"reloaded": 3}))
    out = inference.reload_models.invoke({"model_name": ""})
    assert "reloaded" in out.lower() or "3" in out


def test_reload_models_cancelled(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    out = inference.reload_models.invoke({"model_name": ""})
    assert out == "Cancelled — no action taken."

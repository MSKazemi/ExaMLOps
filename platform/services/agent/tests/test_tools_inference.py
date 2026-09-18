import json
import sys
from pathlib import Path

import httpx
import respx
from skipper import confirm
from skipper.tools import inference

# The agent image carries the examlops package (the OIP v2 client helpers); so do these tests.
_CLI_SRC = Path(__file__).resolve().parents[3] / "cli" / "src"
if str(_CLI_SRC) not in sys.path:
    sys.path.insert(0, str(_CLI_SRC))


@respx.mock
def test_predict_success():
    """Over Open Inference Protocol v2 (ADR 0126). The tool takes a feature *list*, which the old
    /predict (a feature dict) refused with 422 on every call; OIP takes it as one row."""
    route = respx.post("http://localhost:18001/v2/models/JPCP/infer").mock(
        return_value=httpx.Response(
            200,
            json={
                "model_name": "JPCP",
                "model_version": "3",
                "parameters": {"alias": "Production"},
                "outputs": [{"name": "predict", "datatype": "FP64", "shape": [1], "data": [142.7]}],
            },
        )
    )
    out = inference.predict.invoke({"model_name": "JPCP", "features": [1.0, 2.0]})
    assert "142.7" in out and "v3" in out
    sent = json.loads(route.calls.last.request.content)
    assert sent["inputs"] == [
        {"name": "input-0", "shape": [1, 2], "datatype": "FP64", "data": [1.0, 2.0]}
    ]


@respx.mock
def test_predict_a_pinned_version_uses_the_version_path():
    route = respx.post("http://localhost:18001/v2/models/JPCP/versions/5/infer").mock(
        return_value=httpx.Response(
            200, json={"model_version": "5", "outputs": [{"name": "predict", "data": [1.0]}]}
        )
    )
    out = inference.predict.invoke({"model_name": "JPCP", "features": [1.0], "version": "5"})
    assert route.called and "v5" in out


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
    respx.post("http://localhost:18001/reload").mock(
        return_value=httpx.Response(200, json={"reloaded": 3})
    )
    out = inference.reload_models.invoke({"model_name": ""})
    assert "reloaded" in out.lower() or "3" in out


def test_reload_models_cancelled(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    out = inference.reload_models.invoke({"model_name": ""})
    assert out == "Cancelled — no action taken."

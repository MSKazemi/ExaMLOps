import httpx
import respx
from exa_agent import config, confirm
from exa_agent.tools import training


@respx.mock
def test_trigger_retrain_confirmed(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "tok")
    respx.post("http://localhost:18002/retrain").mock(
        return_value=httpx.Response(200, json={"flow_run_id": "abc-123"})
    )
    out = training.trigger_retrain.invoke(
        {"model_name": "JPCP", "dataset_name": "PM100Dataset", "is_dummy": True}
    )
    assert "abc-123" in out


def test_trigger_retrain_no_token(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "")
    out = training.trigger_retrain.invoke({"model_name": "JPCP", "dataset_name": "PM100Dataset"})
    assert "CONTROL_PLANE_TOKEN" in out


def test_trigger_retrain_cancelled(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    out = training.trigger_retrain.invoke({"model_name": "JPCP", "dataset_name": "PM100Dataset"})
    assert out == "Cancelled — no action taken."


@respx.mock
def test_get_retrain_status():
    respx.get("http://localhost:18002/retrain/abc-123").mock(
        return_value=httpx.Response(200, json={"state": "COMPLETED"})
    )
    out = training.get_retrain_status.invoke({"flow_run_id": "abc-123"})
    assert "COMPLETED" in out

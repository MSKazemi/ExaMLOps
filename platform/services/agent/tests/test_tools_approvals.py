import httpx
import respx
from skipper import confirm
from skipper.tools import approvals


@respx.mock
def test_list_pending():
    respx.get("http://localhost:18002/v1/approvals").mock(
        return_value=httpx.Response(200, json=[{"model_id": "jpcp", "status": "pending"}])
    )
    out = approvals.list_pending_approvals.invoke({})
    assert "jpcp" in out


@respx.mock
def test_approve_confirmed(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    respx.post("http://localhost:18002/v1/approvals/jpcp/approve").mock(
        return_value=httpx.Response(200, json={"approved": "jpcp"})
    )
    out = approvals.approve_model.invoke({"model_id": "jpcp"})
    assert "jpcp" in out


def test_reject_cancelled(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    out = approvals.reject_model.invoke({"model_id": "jpcp", "reason": "bad data"})
    assert out == "Cancelled — no action taken."

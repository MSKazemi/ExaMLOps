import httpx
import respx
from skipper import config, confirm
from skipper.tools import training


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


# ── an agent-initiated retrain must leave a trace, whichever agent surface it came from ──
#
# The same action through the MCP surface writes an `mcp`/`retrain_triggered` event and
# `exa retrain` writes an `exa-retrain` one; Skipper wrote nothing. The control plane — the
# one place every caller passes through — cannot record it, because it runs without access to
# the shared platform.db.


@respx.mock
def test_trigger_retrain_is_audited(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(training, "_audit_retrain", lambda *a: seen.append(a))
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "tok")
    respx.post("http://localhost:18002/retrain").mock(
        return_value=httpx.Response(200, json={"flow_run_id": "abc-123"})
    )

    training.trigger_retrain.invoke(
        {"model_name": "JPCP", "dataset_name": "PM100Dataset", "is_dummy": True}
    )

    assert seen == [("JPCP", "PM100Dataset", True, "", "abc-123")]


def test_cancelled_retrain_is_not_audited(monkeypatch):
    """The other direction: an action the operator declined never happened."""
    seen: list[tuple] = []
    monkeypatch.setattr(training, "_audit_retrain", lambda *a: seen.append(a))
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")

    training.trigger_retrain.invoke({"model_name": "JPCP", "dataset_name": "PM100Dataset"})

    assert seen == []


def test_audit_failure_never_fails_the_retrain(monkeypatch):
    """An audit error must not turn a successful retrain into a reported failure."""

    def _boom(*_a, **_k):
        raise RuntimeError("platform.db unreachable")

    monkeypatch.setattr(config, "AGENT_ACTOR", "tester")
    monkeypatch.setitem(
        __import__("sys").modules,
        "examlops.data.audit",
        type("m", (), {"write_audit_event": _boom}),
    )
    training._audit_retrain("JPCP", "PM100Dataset", True, "", "abc-123")  # must not raise

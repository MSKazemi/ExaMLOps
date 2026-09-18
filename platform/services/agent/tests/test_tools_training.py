import sys
from pathlib import Path

import httpx
import respx
from skipper import config, confirm
from skipper.tools import training

# The agent image carries the examlops package (the retrain wait loop lives there); so do these tests.
_CLI_SRC = Path(__file__).resolve().parents[3] / "cli" / "src"
if str(_CLI_SRC) not in sys.path:
    sys.path.insert(0, str(_CLI_SRC))

CID = "v1:retrain:c1"


def _command(state: str, flow_run_id: str | None = None) -> dict:
    return {
        "command_id": CID,
        "state": state,
        "result": {"flow_run_id": flow_run_id} if flow_run_id else None,
        "last_error": None,
        "status_url": f"/v1/commands/{CID}",
    }


def _mock_command_api(final_state: str = "succeeded", flow_run_id: str | None = "abc-123"):
    """POST /v1/retrain accepts (pending); following the command finds ``final_state``."""
    submit = respx.post("http://localhost:18002/v1/retrain").mock(
        return_value=httpx.Response(202, json=_command("pending"))
    )
    respx.get(f"http://localhost:18002/v1/commands/{CID}").mock(
        return_value=httpx.Response(200, json=_command(final_state, flow_run_id))
    )
    return submit


@respx.mock
def test_trigger_retrain_confirmed(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "tok")
    submit = _mock_command_api()
    out = training.trigger_retrain.invoke(
        {"model_name": "JPCP", "dataset_name": "PM100Dataset", "is_dummy": True}
    )
    assert "abc-123" in out
    # One Idempotency-Key per call, so transport retries resolve to one command (P1.7).
    assert submit.calls.last.request.headers.get("Idempotency-Key")


@respx.mock
def test_a_retrain_not_dispatched_yet_is_reported_as_accepted(monkeypatch):
    """Prefect down or admission full: the command stands and will be dispatched, so the agent
    must not report a failure that invites a second submission."""
    from examlops import retrain_command

    monkeypatch.setattr(retrain_command, "DEFAULT_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "tok")
    _mock_command_api(final_state="pending", flow_run_id=None)
    out = training.trigger_retrain.invoke({"model_name": "JPCP", "dataset_name": "PM100Dataset"})
    assert "accepted but not dispatched yet" in out and CID in out


@respx.mock
def test_a_retrain_the_control_plane_gave_up_on_is_an_error(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "tok")
    _mock_command_api(final_state="dead", flow_run_id=None)
    out = training.trigger_retrain.invoke({"model_name": "JPCP", "dataset_name": "PM100Dataset"})
    assert out.startswith("Error:") and "dead" in out


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
    respx.get("http://localhost:18002/v1/runs/abc-123").mock(
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
    _mock_command_api()

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

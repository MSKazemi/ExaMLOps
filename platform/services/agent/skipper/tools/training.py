from __future__ import annotations

from langchain_core.tools import tool

from skipper import config
from skipper.confirm import confirmed_write
from skipper.tools import _http


@tool
def list_pipeline_models() -> str:
    """List models known to the training pipeline and their datasets."""
    data, err = _http.request_json("control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/models")
    if err:
        return err
    return "\n".join(f"- {m.get('model_name')}: {', '.join(m.get('datasets', []))}" for m in data)


def _audit_retrain(
    model_name: str, dataset_name: str, is_dummy: bool, backend_name: str, flow_run_id: str
) -> None:
    """Record an agent-initiated retrain in the platform audit log (best-effort).

    The same action through the MCP surface writes an `mcp`/`retrain_triggered` event, and
    `exa retrain` writes an `exa-retrain` one. Skipper wrote nothing, so whether the platform's
    most consequential action left a trace depended on which door the agent came through. The
    control plane, the one place every door passes through, cannot record it — it runs without
    access to the shared platform.db. Never fails the command: an audit error must not turn a
    successful retrain into a reported failure.
    """
    # Best-effort, but never silent: `retrain_triggered` is an EU-AI-Act Art. 12 required event,
    # and one that never lands is invisible to both the hash chain and the coverage report.
    # `audit_best_effort` keeps the fail-open policy and logs + counts the loss.
    try:
        from examlops.data.audit import audit_best_effort
    except Exception:  # noqa: BLE001 - a deployment without `examlops` installed
        return
    audit_best_effort(
        "skipper",
        config.AGENT_ACTOR,
        "retrain_triggered",
        model_name,
        {
            "dataset": dataset_name,
            "is_dummy": bool(is_dummy),
            "backend": backend_name or None,
            "flow_run_id": flow_run_id,
            "via": "skipper-agent",
        },
    )


def _retrain_summary(model_name, dataset_name, is_dummy=False, backend_name=""):
    return f"Trigger retrain of {model_name} on {dataset_name} (dummy={is_dummy}, backend={backend_name or 'default'})"


@tool
@confirmed_write(_retrain_summary)
def trigger_retrain(
    model_name: str, dataset_name: str, is_dummy: bool = False, backend_name: str = ""
) -> str:
    """Start a Prefect retraining run via the Control Plane.

    Args:
        model_name: Model to retrain (e.g. 'JPCP').
        dataset_name: Dataset class name (e.g. 'PM100Dataset').
        is_dummy: Use synthetic dummy data (safe for testing). Prefer True unless asked otherwise.
        backend_name: Optional dataset backend ('zenodo'/'minio'/'dataplane').
    """
    from examlops.service_auth import control_plane_bearer  # noqa: PLC0415

    if not control_plane_bearer(config.CONTROL_PLANE_TOKEN):
        return "Error: CONTROL_PLANE_TOKEN is not set. Cannot trigger retraining."
    body: dict = {"model_name": model_name, "dataset_name": dataset_name, "is_dummy": is_dummy}
    if backend_name:
        body["backend_name"] = backend_name
    # The command API, waited on until dispatched; one Idempotency-Key per call (P1.6c, P1.7).
    data, err = _http.submit_retrain(body)
    if err or data is None:
        return err or "Error: the control plane returned no answer"
    fid = data.get("flow_run_id")
    if not fid:
        cid = data.get("command_id", "?")
        _audit_retrain(model_name, dataset_name, is_dummy, backend_name, f"command:{cid}")
        return (
            f"Retrain accepted but not dispatched yet (command {cid}, state "
            f"{data.get('state', '?')}); the control plane will dispatch it."
        )
    _audit_retrain(model_name, dataset_name, is_dummy, backend_name, fid)
    return f"Retrain triggered. flow_run_id={fid}. Poll with get_retrain_status('{fid}')."


@tool
def get_retrain_status(flow_run_id: str) -> str:
    """Poll a Prefect retraining run's state.

    Args:
        flow_run_id: The id returned by trigger_retrain.
    """
    data, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/runs/{flow_run_id}"
    )
    if err:
        return err
    return f"flow_run {flow_run_id}: {data.get('state', data)}"


TOOLS = [list_pipeline_models, trigger_retrain, get_retrain_status]

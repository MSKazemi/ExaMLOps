from __future__ import annotations

from langchain_core.tools import tool

from exa_agent import config
from exa_agent.confirm import confirmed_write
from exa_agent.tools import _http


@tool
def list_pipeline_models() -> str:
    """List models known to the training pipeline and their datasets."""
    data, err = _http.request_json("control_plane", "GET", f"{config.CONTROL_PLANE_URL}/models")
    if err:
        return err
    return "\n".join(f"- {m.get('model_name')}: {', '.join(m.get('datasets', []))}" for m in data)


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
    if not config.CONTROL_PLANE_TOKEN:
        return "Error: CONTROL_PLANE_TOKEN is not set. Cannot trigger retraining."
    body: dict = {"model_name": model_name, "dataset_name": dataset_name, "is_dummy": is_dummy}
    if backend_name:
        body["backend_name"] = backend_name
    data, err = _http.request_json(
        "control_plane",
        "POST",
        f"{config.CONTROL_PLANE_URL}/retrain",
        headers={"Authorization": f"Bearer {config.CONTROL_PLANE_TOKEN}"},
        json=body,
    )
    if err:
        return err
    fid = data.get("flow_run_id", "?")
    return f"Retrain triggered. flow_run_id={fid}. Poll with get_retrain_status('{fid}')."


@tool
def get_retrain_status(flow_run_id: str) -> str:
    """Poll a Prefect retraining run's state.

    Args:
        flow_run_id: The id returned by trigger_retrain.
    """
    data, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/retrain/{flow_run_id}"
    )
    if err:
        return err
    return f"flow_run {flow_run_id}: {data.get('state', data)}"


TOOLS = [list_pipeline_models, trigger_retrain, get_retrain_status]

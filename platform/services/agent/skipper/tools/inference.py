from __future__ import annotations

from langchain_core.tools import tool

from skipper import config
from skipper.confirm import confirmed_write
from skipper.tools import _http


@tool
def predict(
    model_name: str, features: list[float], alias: str = "Production", version: str = ""
) -> str:
    """Run inference on a model via Ray Serve.

    Args:
        model_name: Registered model name (e.g. 'JPCP').
        features: Feature vector.
        alias: MLflow alias to target (default 'Production').
        version: Optional explicit version; overrides alias when set.
    """
    # Open Inference Protocol v2 (ADR 0126). /predict wanted a feature dict, so the list this
    # tool takes was refused (422) on every call; OIP takes it as one row of features.
    from examlops import oip_client

    data, err = _http.request_json(
        "ray_serve",
        "POST",
        f"{config.RAY_SERVE_URL}{oip_client.infer_path(model_name, version or None)}",
        json=oip_client.features_request(features, alias=None if version else alias),
    )
    if err:
        return err
    try:
        data = oip_client.result(data)
    except ValueError as exc:
        return f"Error: the model server's answer is not a prediction ({exc})"
    return (
        f"prediction={data.get('prediction')}, model={model_name}, "
        f"version=v{data.get('model_version', '?')}, alias={data.get('alias', alias)}"
    )


@tool
def predict_pipeline(embedding: list[float], num_nodes: int) -> str:
    """Run the full inference pipeline (feature transform + routing) via Ray Serve.

    Args:
        embedding: 384-dim feature embedding.
        num_nodes: Number of HPC nodes for the job.
    """
    data, err = _http.request_json(
        "ray_serve",
        "POST",
        f"{config.RAY_SERVE_URL}/infer-pipeline/infer",
        json={"embedding": embedding, "num_nodes": num_nodes},
    )
    if err:
        return err
    return f"pipeline result: {data}"


@tool
def list_loaded_models() -> str:
    """List models currently loaded in the Ray Serve hot set."""
    data, err = _http.request_json("ray_serve", "GET", f"{config.RAY_SERVE_URL}/models")
    if err:
        return err
    return f"loaded models: {data}"


@tool
@confirmed_write(
    lambda model_name="": f"Reload Ray Serve models from MLflow ({model_name or 'all'})"
)
def reload_models(model_name: str = "") -> str:
    """Hot-reload Production models in Ray Serve from MLflow. Empty model_name reloads all.

    Args:
        model_name: Optional single model to reload; empty reloads everything.
    """
    path = f"/reload/{model_name}" if model_name else "/reload"
    data, err = _http.request_json(
        "ray_serve", "POST", f"{config.RAY_SERVE_URL}{path}", headers=_http.serving_admin_headers()
    )
    if err:
        return err
    return f"reload result: {data}"


TOOLS = [predict, predict_pipeline, list_loaded_models, reload_models]

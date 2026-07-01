from __future__ import annotations

from langchain_core.tools import tool

from skipper import config
from skipper.tools import _http


@tool
def list_models(model_name: str = "") -> str:
    """List registered models in the MLflow registry with versions and active aliases.

    Args:
        model_name: Optional filter; empty returns all models.
    """
    data, err = _http.request_json(
        "mlflow",
        "GET",
        f"{config.MLFLOW_URL}/api/2.0/mlflow/registered-models/search",
        params={"max_results": 100},
    )
    if err:
        return err
    models = data.get("registered_models", [])
    if model_name:
        models = [m for m in models if m.get("name", "").upper() == model_name.upper()]
    if not models:
        return "No models found in the registry."
    lines = []
    for model in models:
        name = model.get("name", "?")
        aliases = {a["alias"]: a["version"] for a in model.get("aliases", []) if a.get("alias")}
        versions = (
            ", ".join(
                f"v{v.get('version')} ({v.get('current_stage', 'None')})"
                for v in model.get("latest_versions", [])
            )
            or "no versions"
        )
        alias_str = ", ".join(f"{k}=v{v}" for k, v in aliases.items()) or "none"
        lines.append(f"- {name}: versions=[{versions}] aliases=[{alias_str}]")
    return "\n".join(lines)


@tool
def describe_model(name: str) -> str:
    """Show metadata for one model: README summary, lifecycle stages, supported datasets.

    Args:
        name: Model name (e.g. 'JPCP').
    """
    meta, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/models/{name}/meta"
    )
    if err:
        return err
    readme, _ = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/models/{name}/readme"
    )
    summary = (readme or {}).get("summary") or (meta or {}).get("summary") or "(no summary)"
    datasets = ", ".join((meta or {}).get("datasets", [])) or "unknown"
    stages = ", ".join((meta or {}).get("stages", [])) or "unknown"
    return f"{name}\n  summary: {summary}\n  datasets: {datasets}\n  stages: {stages}"


@tool
def list_datasets() -> str:
    """List the datasets each registered model supports."""
    data, err = _http.request_json("control_plane", "GET", f"{config.CONTROL_PLANE_URL}/models")
    if err:
        return err
    if not data:
        return "No models registered."
    return "\n".join(f"- {m.get('model_name')}: {', '.join(m.get('datasets', []))}" for m in data)


TOOLS = [list_models, describe_model, list_datasets]
